import subprocess
import sys
import json
import re
import time
from pathlib import Path


def get_base_dir():
    import os
    home = os.environ.get("JARVIS_HOME", "").strip()
    if home:
        return Path(home).expanduser().resolve()
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


BASE_DIR         = get_base_dir()
API_CONFIG_PATH  = BASE_DIR / "config" / "api_keys.json"

# Share the deterministic Android repairs with the orchestrator's coding agent
# (relocating stray sources, reading the manifest launcher, dex class checks) so
# the two surfaces cannot drift. dev_agent stays usable standalone if the
# orchestrator package is not importable.
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))
try:
    from orchestrator.agents.android_support import (
        relocate_stray_sources as _relocate_stray_sources,
        launcher_activity as _launcher_activity,
        apk_has_class as _apk_has_class,
        reconcile_view_ids as _reconcile_view_ids,
        detect_package as _detect_package,
        launcher_name as _launcher_name,
    )
except Exception:                     # pragma: no cover - standalone fallback
    def _relocate_stray_sources(project_dir):        # type: ignore
        return ""

    def _launcher_activity(project_dir):             # type: ignore
        return ""

    def _apk_has_class(apk, fqcn):                   # type: ignore
        return False

    def _reconcile_view_ids(project_dir):            # type: ignore
        return ""

    def _detect_package(project_dir, prefer_activity=""):   # type: ignore
        java_root = Path(project_dir) / "app/src/main/java"
        sources = sorted(java_root.glob("**/*.kt")) + \
            sorted(java_root.glob("**/*.java"))
        acts = [s for s in sources if s.stem.endswith("Activity")]
        chosen = next((s for s in acts if s.stem ==
                       (prefer_activity or "").strip()), None) or \
            next((s for s in acts if s.stem == "MainActivity"), None) or \
            (acts[0] if acts else None)
        pkgs = [".".join(s.relative_to(java_root).parts[:-1]) for s in sources]
        pkgs = [p for p in pkgs if p] or ["com.example.app"]
        shallow = min(len(p.split(".")) for p in pkgs)
        pkg = sorted({p for p in pkgs if len(p.split(".")) == shallow})[0]
        return pkg, (chosen.stem if chosen else "MainActivity")

    def _launcher_name(project_dir, pkg, activity_cls):     # type: ignore
        return f".{activity_cls}"
PROJECTS_DIR     = Path.home() / "Desktop" / "JarvisProjects"
MAX_FIX_ATTEMPTS = 5
# Model choice, timeout and fallback ladder all live in core/gemini.py.
from core import gemini

MODEL_PLANNER    = gemini.SMART
MODEL_WRITER     = gemini.SMART

def _get_api_key() -> str:
    with open(API_CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)["gemini_api_key"]


def _get_model(model_name: str = gemini.SMART):
    """Planning and writing whole files — the reasoning tier, and a long
    deadline because the answer is a source file rather than a sentence.

    When a cloud coding key is configured — OpenRouter (the working DeepSeek)
    or NVIDIA NIM — coding goes through the house Model Router first
    (ModelTask.SIMPLE_CODING), which prefers DeepSeek and falls to NVIDIA's
    chain behind it. So planning/writing/fixing are served by DeepSeek even
    with Gemini's Live rung sitting in front of core/gemini. Falls back to
    `core/gemini`'s SMART ladder, which is also the whole path when neither
    key is set (unchanged behaviour, no probe latency)."""
    class _W:
        def generate_content(self, contents):
            text = _router_coding_text(contents)
            if text:
                return _Text(text)
            resp = gemini.call(contents, tier=model_name, timeout_ms=60000)
            if resp is None:
                raise RuntimeError("every Gemini model on the ladder failed")
            return resp

    return _W()


class _Text:
    """The one attribute every caller of these models reads back."""
    __slots__ = ("text",)

    def __init__(self, text: str) -> None:
        self.text = text


def _nvidia_configured() -> bool:
    """True when an NVIDIA NIM key is present (config/api_keys.json). The
    router is only preferred for coding when it actually has the specialist,
    so an ordinary Gemini-only install keeps its current path."""
    try:
        with open(API_CONFIG_PATH, encoding="utf-8") as f:
            cfg = json.load(f)
        return bool((cfg.get("nvidia_api_key") or "").strip())
    except Exception:
        return False


def _openrouter_configured() -> bool:
    """True when OpenRouter keys exist — the transport the working DeepSeek
    coder is reached through. Kept separate from _nvidia_configured so an
    NVIDIA-only or OpenRouter-only install both behave sanely."""
    try:
        with open(API_CONFIG_PATH, encoding="utf-8") as f:
            cfg = json.load(f)
        return bool(cfg.get("openrouter_keys"))
    except Exception:
        return False


def _router_coding_text(contents) -> str:
    """The Model Router's coding answer — DeepSeek (via OpenRouter) first,
    NVIDIA's NIM chain second — or "" when the router is unavailable. Never
    raises; a router hiccup must not cost the build, and the caller then falls
    back to Gemini."""
    prompt = contents if isinstance(contents, str) else str(contents)
    # Whole-file app generation must not fall to the local 3B, so each rung is
    # PINNED to a cloud provider; the router never widens the choice.
    providers = [p for p, ok in (("deepseek", _openrouter_configured()),
                                 ("nvidia", _nvidia_configured())) if ok]
    if not providers:
        return ""
    try:
        from orchestrator.model_router import (ModelRouter, ModelTask,
                                               RouteOptions)
        router = ModelRouter()
        for provider in providers:
            try:
                res = router.route(
                    prompt, RouteOptions(task=ModelTask.SIMPLE_CODING,
                                         force_provider=provider,
                                         timeout_s=75, deadline_s=150))
            except Exception as e:
                print(f"[DevAgent] ⚠️ {provider} routing failed: {e}")
                continue
            if res.ok and (res.text or "").strip():
                return res.text.strip()
        return ""
    except Exception as e:
        print(f"[DevAgent] ⚠️ model router unavailable, using Gemini: {e}")
        return ""


def _strip_fences(text: str) -> str:
    text = text.strip()
    text = re.sub(r"^```[a-zA-Z]*\r?\n?", "", text)
    text = re.sub(r"\r?\n?```\s*$", "", text)
    return text.strip()


def _is_rate_limit(error: Exception) -> bool:
    msg = str(error).lower()
    return "429" in msg or "quota" in msg or "resource_exhausted" in msg


def _parse_traceback(output: str, project_files: list[str]) -> tuple[str | None, int | None]:

    pattern = re.compile(r'File ["\']([^"\']+\.py)["\'],\s+line\s+(\d+)', re.IGNORECASE)
    matches = pattern.findall(output)

    if not matches:
        # Kotlin/Java compiler errors: 'e: file:///path/File.kt:19:56 text'
        # (gradle + kotlinc format). Without this the fix loop guessed the
        # wrong file — five MainActivity edits for a TodoAdapter error
        # (observed 2026-10-15).
        kpattern = re.compile(
            r'[ew]:\s+file://(\S+?\.(?:kt|java)):(\d+)', re.IGNORECASE)
        k_matches = kpattern.findall(output)
        if not k_matches:
            # javac format: '/abs/path/File.java:30: error: text'
            jpattern = re.compile(
                r'([^\s:]+\.(?:kt|java)):(\d+):\s+error', re.IGNORECASE)
            k_matches = jpattern.findall(output)
        if not k_matches:
            # AAPT2 resource error. Two shapes seen in the wild:
            #   '/abs/.../res/layout/foo.xml:12: error: …'
            #   'com.x.app-mergeDebugResources-31:/layout/foo.xml:17: error: …'
            # Points the fix at the offending LAYOUT, not at the activity.
            rpattern = re.compile(
                r'([^\s:]*\.xml):(\d+):\s*error', re.IGNORECASE)
            k_matches = rpattern.findall(output)
    else:
        k_matches = []

    for raw_path, line_str in reversed(matches):
        raw_name = Path(raw_path).name
        for pf in project_files:
            if Path(pf).name == raw_name or pf == raw_path or raw_path.endswith(pf):
                return pf, int(line_str)

    # kotlinc reports independent errors (no frame stack) — the FIRST one is
    # the primary complaint, so no reversal here.
    for raw_path, line_str in k_matches:
        raw_name = Path(raw_path).name
        for pf in project_files:
            if Path(pf).name == raw_name or pf == raw_path or raw_path.endswith(pf):
                return pf, int(line_str)

    return None, None


def _classify_error(output: str) -> str:

    low = output.lower()

    if any(x in low for x in ("no module named", "modulenotfounderror", "importerror")):
        return "dependency_error"

    if "syntaxerror" in low or "invalid syntax" in low:
        return "syntax_error"

    if "cannot import" in low or "importerror" in low:
        return "import_error"

    # Generated-code errors MUST be checked before the generic "error:"
    # runtime pattern — "error: package R does not exist" contains both, and
    # R is produced by AAPT from the manifest, so an LLM edit of the activity
    # can never fix it. The scaffold must (observed 2026-10-16: three wasted
    # LLM fixes of MainActivity.java for a package mismatch).
    if any(x in low for x in (
        "package r does not exist", "r cannot be resolved",
        "cannot find symbol: class r",
        # A build that "succeeded" but shipped a library-only shell: the module
        # compiled none of the app's sources (they were written outside it).
        # Re-scaffolding relocates them — never an LLM edit of the code.
        "empty shell", "lacks compiled app code",
    )):
        return "scaffold_error"

    # Android resource linking (AAPT2): a missing @string/@color/@style, or a
    # layout that will not compile. Gradle reports it as "Execution failed for
    # task ':app:processDebugResources'" with NO source file, so an edit of the
    # activity can never fix it — the scaffold (which auto-declares missing
    # resources) must. Observed 2026-10-17: five MainActivity rewrites for one
    # processDebugResources failure, each log line truncated to
    # "A failure occurred whil".
    if any(x in low for x in (
        "processdebugresources", "resource linking failed", "aapt:",
        "android resource linking failed", "resource not found",
        "failed to compile resources", "error: resource",
    )) or re.search(r"res/[^\s:]+\.xml:\d+", low):
        return "resource_error"

    # Kotlin/Javac compile errors the model CAN fix by editing code — check
    # before the generic environment bucket so "unresolved reference" is not
    # (wrongly) blamed on the toolchain.
    if any(x in low for x in ("unresolved reference", "compilation error",
                              "compilation failed", "e: file")) \
            or re.search(r"\.(?:kt|java):\d+:\s*(?:error|warning)",
                         output or "", re.IGNORECASE):
        return "compile_error"

    # Process-level failures the LLM cannot fix by editing code, but which
    # MUST NOT be reported as success (2026-10-15: a missing ./gradlew was
    # announced as "Project is working, sir" five times in a row).
    if any(x in low for x in (
        "command not found", "no such file or directory", "errno",
        "is not recognized", "build failed", "execution failed",
        "installation failed", "sdk location not found",
    )) or re.search(r"\b(gradlew?|adb|sdkmanager|javac?|kotlinc)\b[^\n]{0,40}"
                    r"not found", output or "", re.IGNORECASE):
        return "environment_error"

    if any(x in low for x in (
        "traceback", "exception", "error:", "nameerror", "typeerror",
        "attributeerror", "valueerror", "keyerror", "indexerror",
        "zerodivisionerror", "filenotfounderror", "permissionerror",
    )):
        return "runtime_error"

    return "none"


def _has_error(output: str, run_command: str) -> bool:
    
    low = output.lower()

    if "timed out" in low:
        return False

    if not output.strip():
        return False

    error_type = _classify_error(output)
    return error_type not in ("none",)


def _error_signature(output: str) -> str:
    """A stable fingerprint of a build failure, blind to the exact symbol names
    and source positions. Two rounds that keep failing the same way (an
    "Unresolved reference" whose name the fixer reinvents each time) collapse to
    ONE signature, which is what lets the loop notice it is not converging.
    Python line numbers are kept — a different traceback there is real progress."""
    low = (output or "").lower()
    low = re.sub(r"[\w./\\-]+\.(?:kt|java):\d+(?::\d+)?", "<file>", low)
    low = re.sub(r"unresolved reference:?\s*\w*", "unresolved reference", low)
    low = re.sub(r"\s+", " ", low).strip()
    return low[:300]


def _error_excerpt(output: str, limit: int = 1500) -> str:
    """The part of a build log a human (or the voice) needs: Gradle's FAILURE
    block when present, else the raw tail — never the 150-char preview that
    truncated a real error into "A failure occurred whil" (observed 2026-10-17)."""
    text = (output or "").strip()
    if not text:
        return ""
    m = re.search(r"(?m)^(FAILURE:|BUILD FAILED|e: |error: )", text)
    if m:
        text = text[m.start():]
    return text[:limit]


class RateLimitError(Exception):
    pass


def _design_android_spec(description: str) -> str:
    """Write the app spec FIRST (Gemini SMART), then plan and build from it.

    A one-line ask ("make a todo app") makes the planner build the smallest
    thing that compiles — a single static screen. A concrete spec for an
    INTERACTIVE app makes both the planner and the writer produce real screens
    and click handlers. Never fatal: on any failure the caller keeps the
    original description."""
    model = _get_model(MODEL_PLANNER)
    prompt = f"""You are JARVIS's product designer. Turn this one-line request
into a detailed spec for a PROPER INTERACTIVE Android app (Kotlin + XML
layouts). Write requirements only — no code, no JSON.

USER REQUEST:
{description}

Write the spec under these headings, concretely and briefly:
1. PURPOSE — what the app does, in 2-3 sentences.
2. SCREENS — each screen, what it shows and how the user reaches it.
3. INTERACTIONS — every control (button, text field, list, switch) and what it
   DOES when used: the state it updates, the screen it opens, the item it adds
   or removes.
4. DATA & STATE — what the app remembers while running (and across restarts
   when it matters).
5. MUST-HAVE BEHAVIOUR — 3-6 bullets the finished app must demonstrably do.

Rules:
- Design a WORKING, INTERACTIVE app: real click handlers, real user input and
  visible state changes.
- Do NOT describe one static screen that only shows text — that is a failure,
  not an app.
- Prefer 2-4 screens with real navigation when the app suits it.
- Use only AndroidX / Material components.

Spec:"""
    try:
        response = model.generate_content(prompt)
        return _strip_fences(response.text or "").strip()
    except Exception as e:
        if _is_rate_limit(e):
            print("[DevAgent] ⚠️ rate limit while writing the app spec — "
                  "planning from the raw request.")
        else:
            print(f"[DevAgent] ⚠️ could not write the app spec: {e}")
        return ""


def _plan_project(description: str, language: str) -> dict:
    model = _get_model(MODEL_PLANNER)

    android_rules = ""
    if (language or "").strip().lower() == "android":
        android_rules = """
Android-specific rules (IMPORTANT — this is an interactive app, not a demo):
- Plan every screen, adapter, data model and layout the description calls
  for. An interactive app needs its click handlers, list adapters and state
  in real files — do NOT trim the list down to a single activity.
- Plan the layout XML for EVERY activity and fragment, and an item layout for
  any adapter (e.g. app/src/main/res/layout/activity_main.xml,
  fragment_tasks.xml, item_task.xml, activity_add_edit.xml). A layout the code
  references but that is never written does not compile.
- The entry point is the main Activity .kt file (e.g. "MainActivity.kt").
- Write every file under app/src/main/java/<package as folders>/ (Kotlin) or
  app/src/main/res/layout/ (XML) — a Kotlin file written anywhere else is not
  compiled, and the APK then installs but crashes on open.
- Do NOT plan Gradle files, AndroidManifest.xml, strings.xml or colors.xml —
  those are generated by the scaffold.
- "dependencies" are Gradle coordinates (e.g. "androidx.recyclerview:recyclerview"),
  never pip packages.
"""

    prompt = f"""You are a senior software architect. Create a minimal, complete file plan for this project.

Language: {language}
Description: {description}

Return ONLY valid JSON — no markdown, no explanation:
{{
  "project_name": "snake_case_name",
  "entry_point": "main.py",
  "files": [
    {{
      "path": "main.py",
      "description": "Entry point — what it does and which modules it imports",
      "imports": ["utils.helpers", "core.engine"]
    }},
    {{
      "path": "utils/helpers.py",
      "description": "Helper utilities — what functions it exposes",
      "imports": []
    }}
  ],
  "run_command": "python main.py",
  "dependencies": ["requests"]
}}

Critical rules:
1. List files in DEPENDENCY ORDER — files with no imports come first, entry point comes last.
2. The "imports" field must list every other project module this file imports (dot-notation, e.g. "utils.helpers").
3. Keep it minimal — only files truly needed.
4. Entry point must be in the files list.
5. Use relative paths only (e.g. "utils/helpers.py", not absolute paths).
6. Standard library modules (os, sys, json, etc.) do NOT go in "dependencies".
{android_rules}
JSON:"""

    try:
        response = model.generate_content(prompt)
        raw = _strip_fences(response.text)
        return json.loads(raw)
    except json.JSONDecodeError as e:
        raise ValueError(f"Planner returned invalid JSON: {e}\nRaw: {response.text[:300]}")
    except Exception as e:
        if _is_rate_limit(e):
            raise RateLimitError(str(e))
        raise

def _write_file(
    file_info: dict,
    project_description: str,
    all_files: list[dict],
    language: str,
    project_dir: Path,
    already_written: dict[str, str],
) -> str:
    model = _get_model(MODEL_WRITER)

    file_path = file_info["path"]
    file_desc = file_info.get("description", "")
    file_imports = file_info.get("imports", [])

    file_list = "\n".join(
        f"  [{i+1}] {f['path']}: {f.get('description', '')}"
        for i, f in enumerate(all_files)
    )

    dependency_context = ""
    for dep_dotted in file_imports:
        dep_path = dep_dotted.replace(".", "/") + ".py"
        if dep_path in already_written:
            code_snippet = already_written[dep_path][:2000]
            dependency_context += f"\n\n--- {dep_path} (you must import from this) ---\n{code_snippet}"

    lang_rules = ""
    if language.lower() == "python":
        lang_rules = """
Python-specific rules:
- Use type hints for all function signatures.
- Add docstrings for all public functions and classes.
- Use if __name__ == "__main__": guard in the entry point.
- For relative imports within the project, use: from utils.helpers import foo  (match the project structure exactly).
- Do NOT use implicit relative imports (from . import ...) unless it's a proper package with __init__.py.
- If this is a package subdirectory, create __init__.py files where needed."""
    elif language.lower() in ("javascript", "typescript", "js", "ts"):
        lang_rules = """
JS/TS-specific rules:
- Use ES modules (import/export), not CommonJS (require).
- Add JSDoc comments for all exported functions.
- Handle promise rejections with try/catch in async functions."""

    prompt = f"""You are a senior {language} developer writing production-quality code for a real project.

Project goal: {project_description}

Complete project file structure (in dependency order):
{file_list}

{f"Dependencies this file must import from other project files:{dependency_context}" if dependency_context else ""}

Your task: Write the complete, working code for: {file_path}
Purpose of this file: {file_desc}
{f"This file imports from: {', '.join(file_imports)}" if file_imports else "This file has no project-internal imports."}

{lang_rules}

General rules:
- Output ONLY raw code. Absolutely no explanation, no markdown, no triple backticks.
- Write COMPLETE, RUNNABLE code — no placeholders, no "# TODO", no "pass" stubs.
- Every import must either be from the standard library, listed dependencies, or the project files shown above.
- Match import paths EXACTLY to the file paths in the project structure (e.g. if file is "utils/helpers.py", import as "from utils.helpers import ...").
- Use proper error handling (try/except) where I/O or network calls are made.
- The code must work correctly when the project entry point is run from the project root directory.

Code for {file_path}:"""

    try:
        response = model.generate_content(prompt)
        code = _strip_fences(response.text)

        full_path = project_dir / file_path
        full_path.parent.mkdir(parents=True, exist_ok=True)
        full_path.write_text(code, encoding="utf-8")

        print(f"[DevAgent] ✅ Written: {file_path} ({len(code)} chars)")
        return code

    except Exception as e:
        if _is_rate_limit(e):
            raise RateLimitError(str(e))
        raise

def _install_dependencies(dependencies: list[str], project_dir: Path) -> str:
    if not dependencies:
        return "No external dependencies."

    to_install = []
    for dep in dependencies:
        pkg_name = re.split(r"[>=<!]", dep)[0].strip()
        result = subprocess.run(
            [sys.executable, "-m", "pip", "show", pkg_name],
            capture_output=True, text=True
        )
        if result.returncode != 0:
            to_install.append(dep)
        else:
            print(f"[DevAgent] ✓ Already installed: {pkg_name}")

    if not to_install:
        return f"All dependencies already installed: {', '.join(dependencies)}"

    print(f"[DevAgent] 📦 Installing: {to_install}")
    try:
        result = subprocess.run(
            [sys.executable, "-m", "pip", "install"] + to_install,
            capture_output=True, text=True,
            encoding="utf-8", errors="replace",
            timeout=120, cwd=str(project_dir)
        )
        if result.returncode == 0:
            return f"Installed: {', '.join(to_install)}"
        return f"Install warning (non-fatal): {result.stderr[:200]}"
    except subprocess.TimeoutExpired:
        return "Dependency install timed out (non-fatal)."
    except Exception as e:
        return f"Install error (non-fatal): {e}"

def _open_vscode(project_dir: Path) -> bool:
    vscode_candidates = [
        "code",
        rf"C:\Users\{Path.home().name}\AppData\Local\Programs\Microsoft VS Code\bin\code.cmd",
        r"C:\Program Files\Microsoft VS Code\bin\code.cmd",
    ]
    for cmd in vscode_candidates:
        try:
            subprocess.Popen(
                [cmd, str(project_dir)],
                shell=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL
            )
            time.sleep(1.5)
            print(f"[DevAgent] 💻 VSCode opened: {project_dir}")
            return True
        except Exception:
            continue
    return False

# ── Android (APK) support ────────────────────────────────────────────────────
# The machine has JDK 17 but NO gradle and NO Android SDK build-tools by
# default (only platform-tools). scripts/android_bootstrap.sh installs the
# rest. The scaffold below turns the LLM-planned Kotlin sources into a real
# single-module Gradle project so a build is actually possible.

_KNOWN_DEP_VERSIONS = {
    "androidx.appcompat:appcompat": "1.7.0",
    "androidx.core:core-ktx": "1.13.1",
    "com.google.android.material:material": "1.12.0",
    "androidx.recyclerview:recyclerview": "1.3.2",
    "androidx.constraintlayout:constraintlayout": "2.1.4",
    "androidx.room:room-runtime": "2.6.1",
    "androidx.room:room-ktx": "2.6.1",
    "androidx.lifecycle:lifecycle-runtime-ktx": "2.8.4",
    "androidx.activity:activity-ktx": "1.9.2",
}


def _android_sdk() -> Path | None:
    """Locate an Android SDK: env, the bootstrap target, or the system dir."""
    import os
    for cand in (os.environ.get("ANDROID_HOME"),
                 os.environ.get("ANDROID_SDK_ROOT"),
                 str(Path.home() / ".android-sdk"),
                 "/usr/lib/android-sdk"):
        p = Path(cand).expanduser() if cand else None
        if p and p.is_dir():
            return p
    return None


def _gradle_bin() -> str | None:
    import os, shutil
    found = shutil.which("gradle")
    if found:
        return found
    home = Path.home() / ".gradle-dist"
    if home.exists():
        grads = sorted(home.glob("gradle-*/bin/gradle"))
        if grads:
            return str(grads[-1].resolve())
    return None


def _android_run_command(project_dir: Path) -> str | None:
    """A runnable android build command, or None (toolchain missing)."""
    gradlew = project_dir / "gradlew"
    if gradlew.exists():
        return "./gradlew assembleDebug --no-daemon -q"
    gradle = _gradle_bin()
    if gradle:
        return f'"{gradle}" assembleDebug --no-daemon -q'
    return None


def _find_apk(project_dir: Path) -> str | None:
    apks = sorted(project_dir.glob("**/build/outputs/apk/**/*.apk"))
    return str(apks[-1].resolve()) if apks else None


def _desktop_dir() -> Path:
    """The user's Desktop (JARVIS_DESKTOP overrides `~/Desktop`)."""
    import os
    override = os.environ.get("JARVIS_DESKTOP", "").strip()
    base = Path(override).expanduser() if override else Path.home() / "Desktop"
    try:
        base.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    return base


def _unique_workspace(app_name: str) -> Path:
    """`~/Desktop/<App>` — or `<App>-2`, `-3`… if that folder already holds a
    project. Two builds of the same app must not share a folder: each run wipes
    `app/src`, so a concurrent (or repeat) run would delete the other's sources
    mid-build — the "mainManifest doesn't exist" failure observed 2026-10-17."""
    base = _desktop_dir() / re.sub(r"[^\w\-]", "_", app_name)
    if not base.exists() or not any(base.iterdir()):
        return base
    for i in range(2, 100):
        cand = base.with_name(f"{base.name}-{i}")
        if not cand.exists() or not any(cand.iterdir()):
            return cand
    import os
    return base.with_name(f"{base.name}-{os.getpid()}")


def _export_apk_to_desktop(apk: str, project_dir: Path, app_name: str) -> list[str]:
    """Save the built APK on the Desktop: a copy inside the project folder and
    one at `~/Desktop/<App>.apk` so it is one tap away. Returns the paths
    written; never raises."""
    import shutil
    written: list[str] = []
    try:
        apk_path = Path(apk)
        if not apk_path.is_file():
            return written
        in_proj = project_dir / f"{app_name}.apk"
        if apk_path.resolve() != in_proj.resolve():
            shutil.copy2(apk_path, in_proj)
            written.append(str(in_proj))
        on_desktop = _desktop_dir() / f"{app_name}.apk"
        if apk_path.resolve() != on_desktop.resolve():
            shutil.copy2(apk_path, on_desktop)
            written.append(str(on_desktop))
    except Exception as e:
        print(f"[DevAgent] ⚠️ could not save the APK to the Desktop: {e}")
    return written


def _android_apk_real(apk: Path, launcher: str = "") -> bool:
    """A 'successful' gradle run over a module with no compiled sources still
    produces an APK — it carries the *libraries* but none of the app's code, so
    the launcher class is missing and the app installs then crashes on open
    (observed 2026-10-17). Require real code (>4 KB) AND, when known, that the
    launcher class is actually compiled into the APK."""
    import zipfile
    try:
        with zipfile.ZipFile(apk) as z:
            total = sum(zi.file_size for zi in z.infolist()
                        if zi.filename.endswith(".dex"))
    except Exception:
        return False
    if total <= 4096:
        return False
    if launcher and not _apk_has_class(apk, launcher):
        return False
    return True


def _scaffold_android_project(project_dir: Path, dependencies: list[str],
                              entry_activity: str = "") -> str:
    """Write the gradle files the LLM plan never produces (it only writes
    Kotlin/XML sources), with the app package detected from the sources."""
    # package + activity from the written sources — BOTH languages: a Java
    # plan (.java) used to fall back to the default package, so the
    # generated R class landed in com.example.app while the code imported
    # its own → "package R does not exist" forever (observed 2026-10-16).
    #
    # LLM plans also sometimes write sources WITHOUT the app/ module prefix
    # (src/main/java instead of app/src/main/java). Normalize first — a
    # module with no sources builds an empty shell whose dex holds only the
    # R class, and that was announced as success (observed 2026-10-16).
    app_main = project_dir / "app" / "src" / "main"
    alt_main = project_dir / "src" / "main"

    def _has_sources(p: Path) -> bool:
        if not p.exists():
            return False
        return any(p.glob("**/*.kt")) or any(p.glob("**/*.java"))

    if not _has_sources(app_main / "java") and _has_sources(alt_main / "java"):
        import shutil
        app_main.mkdir(parents=True, exist_ok=True)
        for item in ("java", "res", "AndroidManifest.xml"):
            src, dst = alt_main / item, app_main / item
            if src.exists() and not dst.exists():
                shutil.move(str(src), str(dst))
        note_prefix = "[DevAgent] 🛠 normalized plan layout (src/main → app/src/main); "
    else:
        note_prefix = "[DevAgent] 🛠 Android scaffold: "

    # Sources the model dumped outside the module (ui/, data/ at the repo root,
    # each with its own invented package) compile into nothing — move them in.
    reloc_note = _relocate_stray_sources(project_dir)
    if reloc_note:
        note_prefix += reloc_note + "; "

    java_root = project_dir / "app/src/main/java"
    pkg, activity_cls = _detect_package(project_dir, (entry_activity or "").strip())

    deps: list[str] = []
    for d in list(dependencies or []) + [
        "androidx.core:core-ktx", "androidx.appcompat:appcompat",
        "com.google.android.material:material",
        "androidx.recyclerview:recyclerview",
        "androidx.constraintlayout:constraintlayout",
    ]:
        coord = (d or "").strip().strip('"').strip("'")
        if not coord or ":" not in coord or coord in deps:
            continue
        parts = coord.split(":")
        if len(parts) == 2:
            ver = _KNOWN_DEP_VERSIONS.get(coord)
            if not ver:
                continue            # unversioned unknown → skip, base covers it
            coord = f"{coord}:{ver}"
        deps.append(coord)
    has_room = any(":room-" in d for d in deps)
    if has_room:
        deps.append("androidx.room:room-compiler:2.6.1")
    deps_block = "\n".join(f'    implementation("{d}")' for d in deps)
    kapt_line = '\n    kapt("androidx.room:room-compiler:2.6.1")' if has_room else ""
    kapt_plugin = '\n    id("org.jetbrains.kotlin.kapt")' if has_room else ""
    kapt_root = ('\n    id("org.jetbrains.kotlin.kapt") version "1.9.23" apply false'
                 if has_room else "")

    sdk = _android_sdk()

    (project_dir / "settings.gradle.kts").write_text(
        'pluginManagement {\n'
        '    repositories { google(); mavenCentral(); gradlePluginPortal() }\n'
        '}\n'
        'dependencyResolutionManagement {\n'
        '    repositoriesMode.set(RepositoriesMode.FAIL_ON_PROJECT_REPOS)\n'
        '    repositories { google(); mavenCentral() }\n'
        '}\n'
        f'rootProject.name = "{project_dir.name}"\n'
        'include(":app")\n', encoding="utf-8")

    (project_dir / "build.gradle.kts").write_text(
        'plugins {\n'
        '    id("com.android.application") version "8.4.2" apply false\n'
        '    id("org.jetbrains.kotlin.android") version "1.9.23" apply false'
        f'{kapt_root}\n'
        '}\n', encoding="utf-8")

    (project_dir / "gradle.properties").write_text(
        'org.gradle.jvmargs=-Xmx2048m -Dfile.encoding=UTF-8\n'
        'android.useAndroidX=true\n'
        'android.nonTransitiveRClass=true\n'
        'kotlin.code.style=official\n', encoding="utf-8")

    if sdk:
        (project_dir / "local.properties").write_text(
            f"sdk.dir={sdk}\n", encoding="utf-8")

    app_dir = project_dir / "app"
    app_dir.mkdir(parents=True, exist_ok=True)
    (app_dir / "build.gradle.kts").write_text(
        'plugins {\n'
        '    id("com.android.application")\n'
        '    id("org.jetbrains.kotlin.android")'
        f'{kapt_plugin}\n'
        '}\n\n'
        'android {\n'
        f'    namespace = "{pkg}"\n'
        '    compileSdk = 34\n\n'
        '    defaultConfig {\n'
        f'        applicationId = "{pkg}"\n'
        '        minSdk = 24\n'
        '        targetSdk = 34\n'
        '        versionCode = 1\n'
        '        versionName = "1.0"\n'
        '    }\n\n'
        '    compileOptions {\n'
        '        sourceCompatibility = JavaVersion.VERSION_17\n'
        '        targetCompatibility = JavaVersion.VERSION_17\n'
        '    }\n'
        '    kotlinOptions { jvmTarget = "17" }\n'
        '    buildFeatures { viewBinding = true }\n'
        '}\n\n'
        '// kapt drags in com.intellij:annotations:12.0 — a DIFFERENT module\n'
        '// coordinate than org.jetbrains:annotations, so force alone cannot\n'
        '// dedupe it; exclude it (nothing at runtime needs IntelliJ annotations).\n'
        'configurations.all {\n'
        '    resolutionStrategy.force("org.jetbrains:annotations:23.0.0")\n'
        '    exclude(group = "com.intellij", module = "annotations")\n'
        '}\n\n'
        'dependencies {\n'
        f'{deps_block}{kapt_line}\n'
        '}\n', encoding="utf-8")

    # Manifest: launcher activity + any other activity/receiver the plan wrote.
    # Extra ACTIVITIES matter for multi-screen apps: an activity missing here
    # compiles fine but crashes the moment a control starts it. Names are made
    # fully qualified from the source path so sub-package classes resolve too.
    extra_activities = sorted({
        ".".join(src.with_suffix("").relative_to(java_root).parts)
        for src in list(java_root.glob("**/*.kt"))
        + list(java_root.glob("**/*.java"))
        if src.stem.endswith("Activity") and src.stem != activity_cls})
    activity_xml = "\n".join(
        f'        <activity android:name="{a}" android:exported="false"/>'
        for a in extra_activities)
    receivers = []
    for kt in project_dir.glob("app/src/main/java/**/*.kt"):
        if kt.stem.endswith("Receiver") and kt.stem != activity_cls:
            receivers.append(kt.stem)
    receiver_xml = "\n".join(
        f'        <receiver android:name=".{r}" android:exported="false"/>'
        for r in receivers)
    launcher = _launcher_name(project_dir, pkg, activity_cls)
    manifest = f'''<?xml version="1.0" encoding="utf-8"?>
<manifest xmlns:android="http://schemas.android.com/apk/res/android">

    <application
        android:label="@string/app_name"
        android:theme="@style/Theme.AppCompat.Light.NoActionBar"
        android:supportsRtl="true">
        <activity android:name="{launcher}" android:exported="true">
            <intent-filter>
                <action android:name="android.intent.action.MAIN"/>
                <category android:name="android.intent.category.LAUNCHER"/>
            </intent-filter>
        </activity>
{activity_xml}
{receiver_xml}
    </application>

</manifest>
'''
    mf = app_dir / "src/main/AndroidManifest.xml"
    mf.parent.mkdir(parents=True, exist_ok=True)
    mf.write_text(manifest, encoding="utf-8")

    # @string/app_name referenced by the manifest
    res_values = app_dir / "src/main/res/values"
    res_values.mkdir(parents=True, exist_ok=True)
    (res_values / "strings.xml").write_text(
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<resources>\n'
        f'    <string name="app_name">{project_dir.name}</string>\n'
        '</resources>\n', encoding="utf-8")

    # Auto-define resources the LLM's layouts reference but never declared
    # (a missing @string/add_todo fails resource linking — deterministic fix,
    # no LLM round-trip needed).
    import re as _re
    strings_defined = {"app_name"}
    colors_defined = set()
    res_root = app_dir / "src/main/res"
    defined = res_values / "strings.xml"
    if defined.exists():
        strings_defined |= set(_re.findall(
            r'<string name="([\w]+)"', defined.read_text(encoding="utf-8")))
    colors_file = res_values / "colors.xml"
    if colors_file.exists():
        colors_defined |= set(_re.findall(
            r'<color name="([\w]+)"', colors_file.read_text(encoding="utf-8")))
    wanted_strings, wanted_colors = set(), set()
    for xml in res_root.glob("**/*.xml"):
        if res_values in xml.parents:
            continue
        try:
            txt = xml.read_text(encoding="utf-8")
        except Exception:
            continue
        wanted_strings |= set(_re.findall(r"@string/([\w]+)", txt))
        wanted_colors |= set(_re.findall(r"@color/([\w]+)", txt))
    add_s = "".join(
        f'    <string name="{n}">{n.replace("_", " ").strip().capitalize()}</string>\n'
        for n in sorted(wanted_strings - strings_defined))
    add_c = "".join(
        f'    <color name="{n}">#6750A4</color>\n'
        for n in sorted(wanted_colors - colors_defined))
    auto_n = 0
    if add_s or add_c:
        if add_c and not colors_file.exists():
            colors_file.write_text(
                '<?xml version="1.0" encoding="utf-8"?>\n'
                '<resources>\n' + add_c + '</resources>\n', encoding="utf-8")
        elif add_c:
            colors_file.write_text(
                colors_file.read_text(encoding="utf-8")
                .replace("</resources>", add_c + "</resources>"),
                encoding="utf-8")
        if add_s:
            defined.write_text(
                defined.read_text(encoding="utf-8")
                .replace("</resources>", add_s + "</resources>"),
                encoding="utf-8")
        auto_n = (len(wanted_strings - strings_defined)
                  + len(wanted_colors - colors_defined))

    note = (note_prefix
            + f"package {pkg}, activity {launcher}")
    if auto_n:
        note += f" | auto-defined {auto_n} missing resource(s)"
    id_note = _reconcile_view_ids(project_dir)
    if id_note:
        note += " | " + id_note
    if not sdk:
        note += (" | ⚠ no Android SDK found — run "
                 "scripts/android_bootstrap.sh once, then rebuild")
    if not _gradle_bin():
        note += " | ⚠ no gradle — install via scripts/android_bootstrap.sh"
    return note


def _run_project(run_command: str, project_dir: Path, timeout: int = 30) -> str:
    print(f"[DevAgent] 🚀 Running: {run_command}")
    try:
        import shlex
        # shlex, not split(): quoted paths (e.g. a gradle binary under
        # ~/.gradle-dist) must survive; naive split() kept the quote chars
        # and the exec failed with FileNotFoundError every attempt.
        parts = shlex.split(run_command)
        if parts[0].lower() == "python":
            parts[0] = sys.executable

        result = subprocess.run(
            parts,
            capture_output=True, text=True,
            encoding="utf-8", errors="replace",
            timeout=timeout,
            cwd=str(project_dir)
        )

        stdout = result.stdout.strip()
        stderr = result.stderr.strip()

        combined_parts = []
        if stdout:
            combined_parts.append(f"STDOUT:\n{stdout}")
        if stderr:
            combined_parts.append(f"STDERR:\n{stderr}")

        return "\n\n".join(combined_parts) if combined_parts else "Ran with no output."

    except subprocess.TimeoutExpired:
        return f"Timed out after {timeout}s — long-running app (server/GUI) is likely working."
    except FileNotFoundError as e:
        return f"Command not found: {e}"
    except Exception as e:
        return f"Run error: {e}"

def _try_auto_install(error_output: str, project_dir: Path) -> bool:
    """If there is a ModuleNotFoundError, tries to auto-install the missing package."""
    pattern = re.compile(
        r"No module named ['\"]([a-zA-Z0-9_\-\.]+)['\"]", re.IGNORECASE
    )
    match = pattern.search(error_output)
    if not match:
        return False

    pkg = match.group(1).replace("_", "-").split(".")[0]
    print(f"[DevAgent] 🔧 Auto-installing missing package: {pkg}")
    try:
        result = subprocess.run(
            [sys.executable, "-m", "pip", "install", pkg],
            capture_output=True, text=True,
            encoding="utf-8", errors="replace",
            timeout=60, cwd=str(project_dir)
        )
        return result.returncode == 0
    except Exception:
        return False

def _android_resource_context(project_dir: Path) -> tuple[str, list[str]]:
    """(layout XML in full, the ids they declare) — the ground truth for R.id."""
    res_root = project_dir / "app" / "src" / "main" / "res"
    ctx, ids = "", []
    for xml in sorted(res_root.glob("layout/*.xml")):
        try:
            txt = xml.read_text(encoding="utf-8")
        except Exception:
            continue
        ctx += f"\n--- app/src/main/res/layout/{xml.name} ---\n{txt}"
        ids += re.findall(r'android:id\s*=\s*"@\+?id/([\w]+)"', txt)
    return ctx, sorted(set(ids))


def _fix_files(
    error_output: str,
    project_description: str,
    all_files: list[dict],
    file_codes: dict[str, str],
    language: str,
    project_dir: Path,
    entry_point: str,
) -> dict[str, str]:

    model = _get_model(MODEL_PLANNER)

    error_file, error_line = _parse_traceback(error_output, list(file_codes.keys()))
    error_type = _classify_error(error_output)

    # Android: the layouts are the source of truth for R.id names, and they are
    # NOT in file_codes' 1500-char context window — that blind spot is why the
    # fixer guessed a new id every round (2026-10-17). Hand it the real layouts
    # (full) plus the ids they declare, so it can map, not invent.
    android = (language or "").strip().lower() == "android" or \
        error_type in ("compile_error", "resource_error")
    layout_ctx, known_ids = ("", [])
    if android:
        layout_ctx, known_ids = _android_resource_context(project_dir)

    files_to_fix: list[str] = []

    if error_file:
        files_to_fix.append(error_file)
        if error_type == "import_error":
            for fi in all_files:
                if error_file.replace("/", ".").replace(".py", "") in fi.get("imports", []):
                    p = fi["path"]
                    if p not in files_to_fix:
                        files_to_fix.append(p)
    else:
        files_to_fix.append(entry_point)

    updated_codes: dict[str, str] = {}

    for fix_path in files_to_fix:
        current_code = file_codes.get(fix_path, "")

        # Other-file context: the callee's REAL API matters more than anything —
        # a compile error like "Unresolved reference: setStyle" is fixed by
        # matching WatchFaceView's actual methods, which the 1500-char snippet
        # cut off, so the fixer invented names for five rounds (2026-10-18).
        # Put the sibling SOURCES first and give Android a bigger budget.
        other_ctx = ""
        cap = 6000 if android else 1500
        budget = 12000 if android else 3500
        ordered = sorted(
            (fp for fp, code in file_codes.items() if fp != fix_path and code),
            key=lambda p: (0 if p.endswith((".kt", ".java")) else 1, p))
        for fp in ordered:
            code = file_codes[fp]
            snippet = code[:cap] + ("..." if len(code) > cap else "")
            other_ctx += f"\n--- {fp} ---\n{snippet}\n"

        line_hint = f"\nError appears to be near line {error_line} in this file." if (
            error_line and fix_path == error_file
        ) else ""

        android_section = ""
        if android and (layout_ctx or known_ids):
            target_is_layout = fix_path.replace("\\", "/").endswith(".xml")
            fix_rule = (
                "This file IS a layout: fix the XML so it compiles and its ids "
                "match the code. Keep every id the code already uses."
                if target_is_layout else
                "Do not edit the layouts; fix the code to match them."
            )
            android_section = f"""
Layout resources (the real, authoritative views and ids):
{layout_ctx[:6000]}

Ids declared by these layouts: {', '.join(known_ids) or '(none)'}

Android rules (IMPORTANT):
- Every R.id.<name> in the code MUST match an android:id="@+id/<name>" above.
- If a reference is NOT in that list, replace it with the correct EXISTING id
  from the list. NEVER invent a new id — inventing one just fails again.
- {fix_rule}
"""

        prompt = f"""You are an expert {language} debugger. Fix the broken file below.

Project goal: {project_description}

All project files:
{chr(10).join(f"  - {f['path']}: {f.get('description', '')}" for f in all_files)}

Other files for context (read-only — fix only the target file):
{other_ctx[:budget]}
{android_section}
File to fix: {fix_path}{line_hint}
Error type: {error_type}

Error output:
{error_output[:2500]}

Current (broken) code:
{current_code}

Rules:
- Output ONLY the complete fixed code. No explanation, no markdown, no backticks.
- Fix ALL errors visible in the error output.
- Keep all existing correct logic — do not remove working features.
- Ensure import paths match the actual project file structure exactly.
- Do NOT introduce new bugs or remove error handling.

Fixed code for {fix_path}:"""

        try:
            response = model.generate_content(prompt)
            fixed = _strip_fences(response.text)

            full_path = project_dir / fix_path
            full_path.parent.mkdir(parents=True, exist_ok=True)
            full_path.write_text(fixed, encoding="utf-8")

            updated_codes[fix_path] = fixed
            print(f"[DevAgent] 🔧 Fixed: {fix_path}")

        except Exception as e:
            if _is_rate_limit(e):
                raise RateLimitError(str(e))
            print(f"[DevAgent] ⚠️ Could not fix {fix_path}: {e}")

    return updated_codes

def _build_project(
    description: str,
    language: str,
    project_name: str,
    timeout: int,
    speak=None,
    player=None,
) -> str:

    def log(msg: str):
        print(f"[DevAgent] {msg}")
        if player:
            player.write_log(f"[DevAgent] {msg}")

    is_android_lang = (language or "").strip().lower() == "android"

    if is_android_lang:
        # Write the prompt FIRST: Gemini turns the one-liner into a spec for a
        # proper interactive app, and the planner + writer build from that.
        log("Writing the app spec (Gemini) before planning...")
        spec = _design_android_spec(description)
        if spec:
            description = spec
            log(f"App spec written ({len(spec)} chars) — planning from it.")

    log("Planning project structure...")
    try:
        plan = _plan_project(description, language)
    except RateLimitError:
        msg = "Rate limit reached, sir. Please try again in a moment."
        if speak: speak(msg)
        return msg
    except ValueError as e:
        msg = f"Planning failed: {e}"
        if speak: speak(msg)
        return msg

    proj_name    = project_name or plan.get("project_name", "jarvis_project")
    proj_name    = re.sub(r"[^\w\-]", "_", proj_name)
    # Android apps are saved onto the Desktop (~/Desktop/<App>), where the built
    # APK is exported next to them — non-Android projects keep the
    # JarvisProjects home. A folder already holding a project is never reused
    # (see `_unique_workspace`): the stale-source wipe below owns its tree.
    if is_android_lang:
        project_dir = _unique_workspace(proj_name)
    else:
        project_dir = PROJECTS_DIR / proj_name
    project_dir.mkdir(parents=True, exist_ok=True)
    if is_android_lang:
        # Each run replans from scratch — the LLM may pick a DIFFERENT
        # package this time than last time. Keeping stale sources mixes two
        # package trees: R is generated for one, the other's code breaks
        # with "package R does not exist" forever (observed 2026-10-16 on
        # todo_app). A replan owns the tree: wipe the old sources.
        import shutil
        for stale in (project_dir / "app" / "src",
                      project_dir / "app" / "build"):
            if stale.exists():
                shutil.rmtree(stale, ignore_errors=True)

    files        = plan.get("files", [])
    entry_point  = plan.get("entry_point", "main.py")
    run_command  = plan.get("run_command", f"python {entry_point}")
    dependencies = plan.get("dependencies", [])

    log(f"Project: {proj_name} | Files: {len(files)} | Entry: {entry_point}")

    def _dep_sort_key(fi: dict) -> int:
        return len(fi.get("imports", []))

    sorted_files = sorted(files, key=_dep_sort_key)

    file_codes: dict[str, str] = {}

    for file_info in sorted_files:
        file_path = file_info.get("path", "")
        if not file_path:
            continue

        log(f"Writing {file_path}...")
        for attempt in range(2):
            try:
                code = _write_file(
                    file_info=file_info,
                    project_description=description,
                    all_files=files,
                    language=language,
                    project_dir=project_dir,
                    already_written=file_codes,
                )
                file_codes[file_path] = code
                time.sleep(0.4)
                break
            except RateLimitError:
                if attempt == 0:
                    log("Rate limit — waiting 20s...")
                    time.sleep(20)
                else:
                    log(f"Rate limit retry failed for {file_path}, skipping.")
            except Exception as e:
                log(f"Failed to write {file_path}: {e}")
                break

    if not file_codes:
        msg = "I could not write any project files, sir."
        if speak: speak(msg)
        return msg

    lang = (language or "").strip().lower()
    is_android = lang == "android" or "gradlew" in (run_command or "") \
                 or "gradle" in (run_command or "")

    if is_android:
        # Android dependencies are gradle coordinates, NOT pip packages —
        # pip 'androidx.appcompat:appcompat' is an invalid requirement
        # (seen 2026-10-15). The scaffold carries them instead.
        scaffold_note = _scaffold_android_project(project_dir, dependencies,
                                                  Path(entry_point).stem)
        log(scaffold_note)
        run_command = _android_run_command(project_dir) or run_command
        timeout = max(timeout, 900)   # a cold gradle build downloads ~200 MB
    elif dependencies:
        install_result = _install_dependencies(dependencies, project_dir)
        log(install_result)

    _open_vscode(project_dir)

    last_output   = ""
    auto_installs = 0
    scaffold_retries = 0
    seen_signatures: set[str] = set()

    for attempt in range(1, MAX_FIX_ATTEMPTS + 1):
        log(f"Running project (attempt {attempt}/{MAX_FIX_ATTEMPTS})...")
        last_output = _run_project(run_command, project_dir, timeout)
        log(f"Output preview: {_error_excerpt(last_output, 800)}")

        if not _has_error(last_output, run_command):
            if is_android:
                # Gradle exiting 0 proves nothing — the APK artifact is the
                # truth, and it must contain the app's OWN launcher class (a
                # module whose sources never compiled still ships an APK full
                # of library code, which installs and then crashes on open).
                apk = _find_apk(project_dir)
                launcher = _launcher_activity(project_dir)
                if not apk or not _android_apk_real(Path(apk), launcher):
                    log("Gradle reported success but the APK has no compiled "
                        f"app code (launcher {launcher or '?'} missing) — "
                        "treating as failure.")
                    last_output = (last_output + "\nBUILD FAILED: APK lacks "
                                   "compiled app code (empty shell).").strip()
                else:
                    saved = _export_apk_to_desktop(apk, project_dir, proj_name)
                    desktop_apk = next(
                        (p for p in saved if Path(p).parent == _desktop_dir()),
                        saved[-1] if saved else apk)
                    msg = (
                        f"APK built successfully, sir. "
                        f"Built in {attempt} attempt{'s' if attempt > 1 else ''}. "
                        f"Project saved to: {project_dir}. "
                        f"APK on your Desktop: {desktop_apk}"
                    )
                    if speak: speak(msg)
                    saved_note = ("\n".join(f"  {p}" for p in saved)
                                  if saved else apk)
                    return (f"{msg}\n\nAPK: {apk}\n"
                            f"Saved on your Desktop:\n{saved_note}\n"
                            f"Install on a phone: adb install \"{desktop_apk}\"\n\n"
                            f"Output:\n{last_output}")
            else:
                msg = (
                    f"Project '{proj_name}' is working, sir. "
                    f"Built in {attempt} attempt{'s' if attempt > 1 else ''}. "
                    f"Saved to: {project_dir}"
                )
                if speak: speak(msg)
                return f"{msg}\n\nOutput:\n{last_output}"

        if attempt == MAX_FIX_ATTEMPTS:
            break

        error_type = _classify_error(last_output)
        signature = _error_signature(last_output)

        # No-progress guard: an error fingerprint we have already been through
        # comes back — the model is not converging, just cycling through a few
        # broken variants (2026-10-17: five invented R.id names; 2026-10-18:
        # five rewrites of one call site). Stop and report the real error
        # instead of burning the remaining attempts on rewrites that repeat.
        if signature in seen_signatures:
            msg = (f"I couldn't fix the build of '{proj_name}' — the same error "
                   f"keeps coming back after {attempt - 1} fix round(s), sir, so "
                   f"another rewrite won't help. Project is saved at "
                   f"{project_dir}. The build says:\n"
                   f"{_error_excerpt(last_output)}")
            if speak: speak(msg)
            return msg
        seen_signatures.add(signature)

        if error_type == "environment_error" and is_android \
                and (_gradle_bin() is None or _android_sdk() is None):
            # No point asking the LLM to "fix" missing tooling — say so.
            msg = ("The Android build toolchain is not installed on this "
                   "machine, sir. Run scripts/android_bootstrap.sh once "
                   "(installs gradle + SDK build-tools, ~600 MB), then ask "
                   f"me to build the APK again. Project is saved at: "
                   f"{project_dir}")
            if speak: speak(msg)
            return msg
        if error_type in ("scaffold_error", "resource_error") and is_android \
                and scaffold_retries < 2:
            # A resource failure that NAMES a layout is a real code bug — hand
            # it to the fixer (which now sees the layouts). Only a file-less
            # resource failure (a missing @string/@color) or an R-class
            # mismatch is the scaffold's to fix by re-running it.
            named_file, _ = _parse_traceback(last_output, list(file_codes.keys()))
            if not (error_type == "resource_error" and named_file):
                scaffold_retries += 1
                log(f"Re-scaffolding Android project "
                    f"(attempt {scaffold_retries}/2) — {error_type}.")
                scaffold_note = _scaffold_android_project(
                    project_dir, dependencies, Path(entry_point).stem)
                log(scaffold_note)
                _reconcile_view_ids(project_dir)
                continue
        if error_type == "dependency_error" and auto_installs < 3:
            installed = _try_auto_install(last_output, project_dir)
            if installed:
                auto_installs += 1
                log("Missing dependency installed, retrying...")
                time.sleep(1)
                continue

        log(f"Fixing errors (type: {error_type})...")
        try:
            updated = _fix_files(
                error_output=last_output,
                project_description=description,
                all_files=files,
                file_codes=file_codes,
                language=language,
                project_dir=project_dir,
                entry_point=entry_point,
            )
            file_codes.update(updated)
            time.sleep(1)
        except RateLimitError:
            msg = "Rate limit reached during fix. Project saved, check it manually in VSCode."
            if speak: speak(msg)
            return msg
        except Exception as e:
            log(f"Fix step failed: {e}")

    msg = (
        f"I couldn't fully fix '{proj_name}' after {MAX_FIX_ATTEMPTS} attempts, sir. "
        f"Project is saved at {project_dir} — open it in VSCode and check manually."
    )
    if speak: speak(msg)
    return f"{msg}\n\nLast error:\n{_error_excerpt(last_output)}"


def dev_agent(
    parameters: dict,
    response=None,
    player=None,
    session_memory=None,
    speak=None,
) -> str:
    p            = parameters or {}
    description  = p.get("description", "").strip()
    language     = p.get("language", "python").strip()
    project_name = p.get("project_name", "").strip()
    timeout      = int(p.get("timeout", 30))

    if not description:
        return "Please describe the project you want me to build, sir."

    return _build_project(
        description  = description,
        language     = language,
        project_name = project_name,
        timeout      = timeout,
        speak        = speak,
        player       = player,
    )


# ── Tool declaration (auto-discovered by core/action_loader.py) ──────────────
TOOL = {
    "name": "dev_agent",
    "description": "Builds complete multi-file projects from scratch: plans, writes files, installs deps, opens VSCode, runs and fixes errors. For Android apps it scaffolds a Gradle project, builds an APK and saves the project + APK on the Desktop.",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "description": {
                "type": "STRING",
                "description": "What the project should do"
            },
            "language": {
                "type": "STRING",
                "description": "Programming language (default: python)"
            },
            "project_name": {
                "type": "STRING",
                "description": "Optional project folder name"
            },
            "timeout": {
                "type": "INTEGER",
                "description": "Run timeout in seconds (default: 30)"
            }
        },
        "required": [
            "description"
        ]
    },
    "handler": dev_agent,
}
