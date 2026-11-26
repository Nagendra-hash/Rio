"""
orchestrator/agents/android_support.py — deterministic Android project support
for the coding agent (and the voice DevAgent).

WHY THIS EXISTS
    An LLM can write Kotlin sources, XML layouts and business logic, but it is
    bad at the *plastic* around them: a correct Gradle build (AGP + Kotlin
    plugin versions, `namespace`, `local.properties`, a manifest that names the
    launcher activity, and the `@string`/`@color` resources the layouts
    reference). Observed failures: "package R does not exist" forever, an empty
    shell APK announced as success, `./gradlew` missing. So the *scaffold* is
    deterministic code here, exactly like actions/dev_agent.py learned to do.

    This module is the single source of truth for:
      * where a newly created app is saved   → the Desktop (`~/Desktop/<App>/`)
      * what "make me an Android app" means   → intent + name extraction
      * the Gradle skeleton the sources need  → scaffold_project()
      * how the built APK is exported         → export_to_desktop()

    stdlib only, no core/ imports: it stays importable and testable offline,
    the same convention the toolbox follows.

DESKTOP CONVENTION
    Newly created Android apps are saved to `~/Desktop/<AppName>/` (the project
    folder), and the built APK is copied to BOTH `~/Desktop/<AppName>/<App>.apk`
    and `~/Desktop/<AppName>.apk` so it is one tap away on the desktop. Override
    the desktop root with `JARVIS_DESKTOP` if `~/Desktop` is not where you want
    them.
"""
from __future__ import annotations

import os
import re
import shutil
import zipfile
from pathlib import Path
from typing import Optional

# ── intent / naming vocabulary ───────────────────────────────────────────────
# Android signals — any one of these means "this is about an Android app".
ANDROID_KEYS = ("android", "kotlin", "apk", "gradle", "play store",
                "playstore", "androidx")
# Verbs that mean "create something new". Hinglish included: the house voice
# path routinely takes "…app banao / banado / bana do".
CREATE_VERBS = ("create", "make", "build", "new", "generate", "scaffold",
                "develop", "start", "write", "code", "banao", "banado",
                "bana do", "bana", "banao.", "design", "i want", "i need",
                "give me")
# Verbs that mean "an app already exists" — then we must inspect a repo, not
# silently scaffold a brand-new project over the user's code.
EXISTING_VERBS = ("fix", "debug", "patch", "refactor", "update", "edit",
                  "modify", "repair", "the bug in", "compile error in",
                  "keeps crashing", "crash", "thik karo", "theek karo",
                  "sahi karo")

# Fallback app-name hints, by topic keyword.
_TOPIC_NAMES = (
    ("todo", "TaskApp"), ("to-do", "TaskApp"), ("task", "TaskApp"),
    ("note", "NoteApp"), ("calculator", "Calculator"),
    ("weather", "WeatherApp"), ("timer", "TimerApp"),
    ("chat", "ChatApp"), ("quiz", "QuizApp"), ("music", "MusicApp"),
    ("fitness", "FitnessApp"), ("habit", "HabitApp"),
    ("expense", "ExpenseApp"), ("budget", "BudgetApp"),
    ("reminder", "ReminderApp"), ("calendar", "CalendarApp"),
    ("game", "GameApp"), ("camera", "CameraApp"), ("scanner", "ScannerApp"),
)

DEFAULT_APP_NAME = "JarvisApp"

# The files scaffold_project() owns (always (re)written). Used for the report
# and for rollback of a brand-new project.
SCAFFOLD_FILES = (
    "settings.gradle.kts", "build.gradle.kts", "gradle.properties",
    "local.properties", "app/build.gradle.kts",
    "app/src/main/AndroidManifest.xml",
    "app/src/main/res/values/strings.xml",
    "app/src/main/res/values/colors.xml",
)


def scaffold_paths() -> list[str]:
    """Repo-relative paths the scaffold writes/overwrites."""
    return list(SCAFFOLD_FILES)

# Versions the house scaffold is known to build with (JDK 17, AGP 8.4.x).
AGP_VERSION = "8.4.2"
KOTLIN_VERSION = "1.9.23"
COMPILE_SDK = 34
MIN_SDK = 24
TARGET_SDK = 34
ROOM_VERSION = "2.6.1"

# Gradle coordinates the scaffold always includes (the androidx baseline every
# simple Android app needs), plus known versions for unversioned coordinates an
# LLM plan may mention.
BASE_DEPS = (
    "androidx.core:core-ktx:1.13.1",
    "androidx.appcompat:appcompat:1.7.0",
    "com.google.android.material:material:1.12.0",
    "androidx.recyclerview:recyclerview:1.3.2",
    "androidx.constraintlayout:constraintlayout:2.1.4",
)
KNOWN_DEP_VERSIONS = {
    "androidx.appcompat:appcompat": "1.7.0",
    "androidx.core:core-ktx": "1.13.1",
    "com.google.android.material:material": "1.12.0",
    "androidx.recyclerview:recyclerview": "1.3.2",
    "androidx.constraintlayout:constraintlayout": "2.1.4",
    "androidx.room:room-runtime": ROOM_VERSION,
    "androidx.room:room-ktx": ROOM_VERSION,
    "androidx.lifecycle:lifecycle-runtime-ktx": "2.8.4",
    "androidx.activity:activity-ktx": "1.9.2",
}

# Gradle invocations the permission layer allows the agent to run.
GRADLE_ALLOWED_TASKS = {"assembledebug", "assemblerelease", "build", "clean",
                        "tasks", "help", "--version", "--help", "-v", "-h"}
GRADLE_ALLOWED_FLAGS = {"--no-daemon", "--offline", "--quiet", "-q", "-i",
                        "--info", "--stacktrace", "--console", "plain", "rich",
                        "auto", "-d", "--debug"}


# ── where apps are saved ─────────────────────────────────────────────────────
def desktop_dir() -> Path:
    """The user's Desktop, or `JARVIS_DESKTOP` if the environment overrides it.

    `~/Desktop` is created on demand so a headless box still ends up with the
    project exactly where the user was told it would be."""
    override = os.environ.get("JARVIS_DESKTOP", "").strip()
    base = Path(override).expanduser() if override else Path.home() / "Desktop"
    base.mkdir(parents=True, exist_ok=True)
    return base


def unique_workspace(app_name: str) -> Path:
    """`~/Desktop/<App>` — or `<App>-2`, `-3`… if that folder already holds a
    project, so a new build never clobbers the user's previous one."""
    base = desktop_dir() / sanitize_app_name(app_name)
    if not base.exists() or not any(base.iterdir()):
        return base
    for i in range(2, 100):
        cand = base.with_name(f"{base.name}-{i}")
        if not cand.exists() or not any(cand.iterdir()):
            return cand
    return base.with_name(f"{base.name}-{os.getpid()}")


# ── intent detection + naming ────────────────────────────────────────────────
def is_android_request(text: str) -> bool:
    """True when `text` asks to CREATE a new Android app.

    Deliberately conservative: an existing-app verb (fix/debug/refactor…)
    without a create verb returns False, so "fix my android app" still goes
    down the inspect-an-existing-repo path instead of scaffolding over it."""
    low = (text or "").lower()
    if not any(k in low for k in ANDROID_KEYS):
        return False
    if any(k in low for k in EXISTING_VERBS):
        # "fix / debug / …" wins unless the text ALSO says create.
        return any(k in low for k in ("create", "make", "new", "generate",
                                      "scaffold", "banao", "banado"))
    return any(k in low for k in CREATE_VERBS)


def sanitize_app_name(name: str) -> str:
    """A folder- and package-safe PascalCase-ish app name."""
    raw = re.sub(r"[^A-Za-z0-9 _\-]", "", str(name or "")).strip()
    words = [w for w in re.split(r"[\s_\-]+", raw) if w]
    if not words:
        return DEFAULT_APP_NAME
    pascal = "".join(w[:1].upper() + w[1:] for w in words)
    if pascal[0].isdigit():
        pascal = "App" + pascal
    return pascal[:40]


def detect_app_name(text: str) -> str:
    """Pull an app name out of a request, or fall back to a topic hint."""
    text = text or ""
    # called/named X — prefer a quoted name (may contain spaces).
    m = re.search(r"(?:called|named|name\s+it|titled)\s+[\"']"
                  r"([A-Za-z][\w\-. ]{1,40}?)[\"']", text, re.IGNORECASE)
    if m:
        return sanitize_app_name(m.group(1))
    # …or an unquoted name, which stops at a connective word.
    m = re.search(r"(?:called|named|name\s+it|titled)\s+"
                  r"([A-Za-z][\w\-. ]{1,40}?)"
                  r"(?:\s+(?:app|application|for|to|that|which|with)\b|,|\.|$)",
                  text, re.IGNORECASE)
    if m:
        return sanitize_app_name(m.group(1))
    # a standalone quoted name: an app "Quiet Notes"
    m = re.search(r"[\"']([A-Za-z][\w\-. ]{1,40})[\"']", text)
    if m:
        return sanitize_app_name(m.group(1))
    low = text.lower()
    for key, name in _TOPIC_NAMES:
        if key in low:
            return name
    return DEFAULT_APP_NAME


def to_package_segment(app_name: str) -> str:
    """`ExpenseBook` / `abcd` / `my_app` → a valid lowercase package segment."""
    seg = re.sub(r"[^a-z0-9]", "", str(app_name or "").lower())
    if not seg or not seg[0].isalpha():
        seg = "app" + seg
    return seg or "app"


# ── the app spec: the "prompt" the design model writes before the build ───────
# A one-line request ("make a todo app") makes the coder build the smallest
# thing that compiles — a single static screen. So BEFORE planning we ask
# Gemini (the SMART tier) to turn that line into a concrete spec for an
# INTERACTIVE app, and hand THAT spec to the coding agent as its requirements.
# `spec_prompt` is the prompt for the design model; the two surfaces that build
# Android apps (the orchestrator's coding agent and the voice DevAgent) share
# this wording so both produce the same, richer class of app.
SPEC_SYSTEM = (
    "You are JARVIS's product designer. You turn a one-line app request into "
    "a concrete, buildable specification for an INTERACTIVE Android app. You "
    "write requirements only — never code, never JSON. Answer in plain text."
)

SPEC_PROMPT = """Expand this request into a detailed spec for a PROPER
INTERACTIVE Android app (Kotlin + XML layouts).

USER REQUEST:
{request}

APP NAME: {app_name}

Write the spec under these headings, concretely and briefly:
1. PURPOSE — what the app does, in 2-3 sentences.
2. SCREENS — each screen, what it shows and how the user reaches it.
3. INTERACTIONS — every control (button, text field, list, switch) and what it
   DOES when used: the state it updates, the screen it opens, the item it
   adds or removes.
4. DATA & STATE — what the app remembers while running (and across restarts
   when it matters).
5. MUST-HAVE BEHAVIOUR — 3-6 bullets the finished app must demonstrably do.

RULES:
- Design a WORKING, INTERACTIVE app: real click handlers, real user input and
  visible state changes.
- Do NOT describe one static screen that only shows text — that is a failure,
  not an app.
- Prefer 2-4 screens with real navigation when the app suits it.
- Use only AndroidX / Material components.
- No code, no file paths, no JSON — just the spec.
"""


def spec_prompt(request_text: str, app_name: str = "JarvisApp") -> str:
    """The prompt that asks the design model (Gemini) to write the app spec."""
    return SPEC_PROMPT.format(
        request=(request_text or "")[:2000],
        app_name=sanitize_app_name(app_name or "JarvisApp"))


# ── toolchain discovery ──────────────────────────────────────────────────────
def sdk_path() -> Optional[Path]:
    """Locate an Android SDK: env, the bootstrap target, or the system dirs."""
    for cand in (os.environ.get("ANDROID_HOME"),
                 os.environ.get("ANDROID_SDK_ROOT"),
                 str(Path.home() / ".android-sdk"),
                 str(Path.home() / "Android" / "Sdk"),
                 "/usr/lib/android-sdk"):
        p = Path(cand).expanduser() if cand else None
        if p and p.is_dir():
            return p
    return None


def gradle_bin() -> Optional[str]:
    """The gradle executable: PATH, then the two bootstrap layouts."""
    found = shutil.which("gradle")
    if found:
        return found
    for home in (Path.home() / ".gradle-dist", Path.home()):
        if home.is_dir():
            grads = sorted(home.glob("gradle-*/bin/gradle"))
            if grads:
                return str(grads[-1].resolve())
    return None


def toolchain_ready() -> tuple[bool, str]:
    """(ready, reason) for actually building an APK on this machine."""
    if not gradle_bin():
        return False, ("gradle is not installed — run "
                       "scripts/android_bootstrap.sh once")
    if not sdk_path():
        return False, ("no Android SDK found — run "
                       "scripts/android_bootstrap.sh once")
    return True, ""


# ── project introspection ────────────────────────────────────────────────────
def is_android_project(root: str | os.PathLike) -> bool:
    """A folder is an Android project if it has a Gradle build or Kotlin/Java
    sources under a `src/main` tree."""
    root = Path(root)
    if not root.is_dir():
        return False
    if (root / "settings.gradle").exists() or \
            (root / "settings.gradle.kts").exists() or \
            (root / "build.gradle").exists() or \
            (root / "build.gradle.kts").exists():
        return True
    for pat in ("**/AndroidManifest.xml", "app/src/main/java/**/*.kt",
                "app/src/main/java/**/*.java", "**/*.kts"):
        try:
            if next(root.glob(pat), None) is not None:
                return True
        except Exception:
            continue
    return False


def find_apk(root: str | os.PathLike) -> Optional[Path]:
    apks = sorted(Path(root).glob("**/build/outputs/apk/**/*.apk"))
    return apks[-1].resolve() if apks else None


def apk_looks_real(apk: str | os.PathLike,
                   required_class: str = "") -> bool:
    """A gradle run over a module with no compiled sources still produces an
    APK — it packages the *libraries* but none of the app's code, so the
    launcher class is absent. Such an APK installs fine and then crashes with
    ClassNotFoundException the moment it is opened (observed 2026-10-17:
    TodoListApp's sources sat outside the module, the 15 MB shell passed a
    bytes-only check, and the app would not start).

    So: require real code in the dex (>4 KB decoded) AND, when the caller knows
    the launcher, that the launcher class is actually compiled in."""
    try:
        with zipfile.ZipFile(apk) as z:
            total = sum(zi.file_size for zi in z.infolist()
                        if zi.filename.endswith(".dex"))
    except Exception:
        return False
    if total <= 4096:
        return False
    if required_class and not apk_has_class(apk, required_class):
        return False
    return True


def apk_has_class(apk: str | os.PathLike, fqcn: str) -> bool:
    """True when `<fqcn>` is compiled into the APK's dex. Dex stores class
    names as `Lpkg/Class;`, so a byte search finds it without a dex parser."""
    fqcn = (fqcn or "").strip()
    if not fqcn or "." not in fqcn:
        return False
    wanted = ("L" + fqcn.replace(".", "/") + ";").encode("ascii", "ignore")
    try:
        with zipfile.ZipFile(apk) as z:
            for zi in z.infolist():
                if zi.filename.endswith(".dex") and wanted in z.read(zi):
                    return True
    except Exception:
        return False
    return False


# ── the deterministic scaffold ───────────────────────────────────────────────
def _has_sources(p: Path) -> bool:
    if not p.exists():
        return False
    return next(p.glob("**/*.kt"), None) is not None or \
        next(p.glob("**/*.java"), None) is not None


def normalize_layout(project_dir: Path) -> str:
    """LLM plans sometimes write `src/main/...` instead of `app/src/main/...`.
    Move the tree into the module Gradle expects; a module with no sources
    builds an empty shell APK."""
    app_main = project_dir / "app" / "src" / "main"
    alt_main = project_dir / "src" / "main"
    if not _has_sources(app_main / "java") and _has_sources(alt_main / "java"):
        app_main.mkdir(parents=True, exist_ok=True)
        for item in ("java", "res", "AndroidManifest.xml", "assets"):
            src, dst = alt_main / item, app_main / item
            if src.exists() and not dst.exists():
                shutil.move(str(src), str(dst))
        return "normalized plan layout (src/main → app/src/main); "
    return ""


# ── repair sources the model dumped OUTSIDE the module ───────────────────────
# The model is told to write every source under app/src/main/java/<package>/,
# and sometimes writes them somewhere else entirely — at the repo root (ui/,
# data/), with a DIFFERENT package in each file, e.g. an app whose files
# declared com.example.taskmanager.data, com.example.vscodetasks.ui and even
# `package data` (observed 2026-10-17, TodoListApp). Gradle then compiles none
# of them, the build "succeeds" with a library-only shell, and the APK installs
# but crashes on open. `relocate_stray_sources` moves those files into the
# module and forces ONE package, so they compile and R resolves.
_MODULE_TOP_DIRS = {"app", "build", ".gradle", "gradle", ".git", ".idea",
                    ".kotlin", "gradlew", "gradlew.bat"}
_PKG_RE = re.compile(r"(?m)^[ \t]*package[ \t]+[A-Za-z_][\w.]*[ \t]*$")
_R_IMPORT_RE = re.compile(r"(?m)^[ \t]*import[ \t]+[A-Za-z_][\w.]*\.R[ \t]*$")


def _stray_sources(project_dir: Path) -> list[Path]:
    """Kotlin/Java files that are NOT inside the app module's source set."""
    out: list[Path] = []
    for ext in ("*.kt", "*.java"):
        for p in project_dir.rglob(ext):
            rel = p.relative_to(project_dir)
            if rel.parts[0] in _MODULE_TOP_DIRS:
                continue
            if any(part in ("build", ".gradle", ".kotlin")
                   for part in rel.parts[:-1]):
                continue
            out.append(p)
    return sorted(set(out))


def _declared_package(text: str) -> str:
    m = re.search(r"(?m)^[ \t]*package[ \t]+([A-Za-z_][\w.]*)", text)
    return m.group(1) if m else ""


def _pick_package(stray: list[Path], project_dir: Path) -> str:
    """ONE package for the whole app: the entry activity's if it declares a
    sane one, else the most common, else a name derived from the folder."""
    def _sane(pkg: str) -> bool:
        return pkg.count(".") >= 1 and pkg.split(".")[0] not in ("data", "ui")
    entry = next((p for p in stray if p.stem == "MainActivity"), None)
    if entry is not None:
        pkg = _declared_package(entry.read_text(encoding="utf-8",
                                                errors="replace"))
        if _sane(pkg):
            return pkg
    counts: dict[str, int] = {}
    for p in stray:
        pkg = _declared_package(p.read_text(encoding="utf-8",
                                            errors="replace"))
        if _sane(pkg):
            counts[pkg] = counts.get(pkg, 0) + 1
    if counts:
        return max(counts, key=counts.get)
    seg = to_package_segment(project_dir.name)
    return f"com.example.{seg}"


def _force_package(text: str, pkg: str) -> str:
    """Rewrite the file's package (or add one) and fix any `<x>.R` import so
    every file agrees on the namespace the R class is generated in."""
    if _PKG_RE.search(text):
        text = _PKG_RE.sub(f"package {pkg}", text, count=1)
    else:
        text = f"package {pkg}\n\n" + text
    return _R_IMPORT_RE.sub(f"import {pkg}.R", text)


def _rewrite_own_imports(text: str, class_names: set, pkg: str) -> str:
    """Point the model's cross-file imports at the single package. Files
    imported each other under several invented packages; identifying the app's
    own classes by name (a file `Task.kt` defines `Task`) lets us re-point
    exactly those imports and leave library imports alone. Generated
    `<x>.databinding.*` classes follow the namespace too, so those are
    re-pointed as well."""
    def _sub(m: re.Match) -> str:
        dotted = m.group(2)
        name = dotted.split(".")[-1]
        if ".databinding." in "." + dotted:
            return f"{m.group(1)}{pkg}.databinding.{name}"
        if name in class_names:
            return f"{m.group(1)}{pkg}.{name}"
        return m.group(0)

    return re.sub(r"(?m)^([ \t]*import[ \t]+)([A-Za-z_][\w.]*)", _sub, text)


def relocate_stray_sources(project_dir: str | os.PathLike) -> str:
    """Move stray sources into app/src/main/java and force one package.
    No-op (returns "") when the module already has sources under its source
    set, which is the normal, correct case."""
    project_dir = Path(project_dir)
    java_root = project_dir / "app/src/main/java"
    if _has_sources(java_root):
        return ""
    stray = _stray_sources(project_dir)
    if not stray:
        return ""
    stray.sort(key=lambda p: (0 if p.stem == "MainActivity" else 1, str(p)))
    texts = {p: p.read_text(encoding="utf-8", errors="replace") for p in stray}
    pkg = _pick_package(stray, project_dir)
    class_names = {p.stem for p in stray}
    pkg_dir = java_root / Path(*pkg.split("."))
    pkg_dir.mkdir(parents=True, exist_ok=True)
    moved = 0
    for src in stray:
        text = _force_package(texts[src], pkg)
        text = _rewrite_own_imports(text, class_names, pkg)
        dst = pkg_dir / src.name
        if dst.exists() and dst.resolve() != src.resolve():
            tag = src.parent.name or "extra"
            dst = pkg_dir / f"{src.stem}{tag.title()}{src.suffix}"
        dst.write_text(text, encoding="utf-8")
        try:
            src.unlink()
        except OSError:
            pass
        moved += 1
    return (f"relocated {moved} source file(s) that were outside the module "
            f"into app/src/main/java (single package {pkg}); they had not been "
            f"compiled")


def _namespace_of(project_dir: Path, fallback: str) -> str:
    """The AGP namespace from app/build.gradle.kts, else `fallback`."""
    try:
        txt = (project_dir / "app/build.gradle.kts").read_text(
            encoding="utf-8", errors="replace")
        m = re.search(r'namespace\s*=\s*"([^"]+)"', txt)
        if m:
            return m.group(1)
    except OSError:
        pass
    return fallback


def launcher_activity(project_dir: str | os.PathLike) -> str:
    """The launcher activity's fully-qualified name, read from the on-disk
    manifest (the file the APK's launcher entry is built from), so callers can
    check the class really made it into the APK. Falls back to detection."""
    project_dir = Path(project_dir)
    pkg, act = detect_package(project_dir)
    try:
        text = (project_dir / "app/src/main/AndroidManifest.xml").read_text(
            encoding="utf-8", errors="replace")
    except OSError:
        return f"{pkg}.{act}"
    ns = _namespace_of(project_dir, pkg)
    for block in re.split(r"(?=<activity\b)", text):
        if "<activity" not in block or "LAUNCHER" not in block:
            continue
        m = re.search(r'android:name\s*=\s*"([^"]+)"', block)
        if not m:
            continue
        name = m.group(1)
        if name.startswith("."):
            return ns + name
        return name
    return f"{pkg}.{act}"


def _app_package(java_root: Path, sources: list[Path]) -> str:
    """The app's OWN package — where the generated R class must land.

    NOT the anchor file's package: a plan may put its only activity in a
    subpackage (…/watchface/ui/SettingsActivity.kt) while the app root is the
    parent (…/watchface) — anchoring on the activity set the namespace to
    `com.example.watchface.ui`, so every `import com.example.watchface.R`
    failed (observed 2026-10-18, five-source analog-clock app).

    Strongest signal first: an explicit `import <pkg>.R`. Then the shallowest
    package in the tree (the root), most common wins ties."""
    for src in sources:
        try:
            text = src.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        m = re.search(r"^\s*import\s+([\w.]+)\.R\s*$", text, re.MULTILINE)
        if m:
            return m.group(1)
    pkgs = [".".join(s.relative_to(java_root).parts[:-1]) for s in sources]
    pkgs = [p for p in pkgs if p]
    if not pkgs:
        return ""
    shallow = min(len(p.split(".")) for p in pkgs)
    cands = [p for p in pkgs if len(p.split(".")) == shallow]
    return sorted(set(cands), key=lambda p: (-cands.count(p), p))[0]


def detect_package(project_dir: Path,
                   prefer_activity: str = "") -> tuple[str, str]:
    """(package, activity_class) from the written sources — both .kt and .java,
    so the generated R class lands in the package the code imports.

    The launcher is chosen carefully: the plan's declared entry activity wins,
    then a class literally named `MainActivity`, then the first `*Activity`.
    Picking alphabetically-first would make `AddEditActivity` the launcher of a
    multi-screen app."""
    java_root = project_dir / "app/src/main/java"
    sources = sorted(java_root.glob("**/*.kt")) + sorted(java_root.glob("**/*.java"))
    activities = [s for s in sources if s.stem.endswith("Activity")]
    chosen: Optional[Path] = None
    want = (prefer_activity or "").strip()
    if want:
        chosen = next((s for s in activities if s.stem == want), None)
    if chosen is None:
        chosen = next((s for s in activities if s.stem == "MainActivity"), None)
    if chosen is None and activities:
        chosen = activities[0]
    pkg = _app_package(java_root, sources) or "com.example.app"
    return pkg, (chosen.stem if chosen is not None else "MainActivity")


def launcher_name(project_dir: Path, pkg: str, activity_cls: str) -> str:
    """The manifest `android:name` for the launcher: `.Foo` when the activity
    sits in the app's package, else its fully-qualified name (an activity in a
    subpackage is NOT reachable as `.Foo`)."""
    java_root = project_dir / "app/src/main/java"
    for src in getattr(java_root, "glob")("**/*.kt"):
        if src.stem == activity_cls:
            rel = ".".join(src.relative_to(java_root).parts[:-1])
            return f".{activity_cls}" if rel == pkg else f"{rel}.{activity_cls}"
    return f".{activity_cls}"


def _resolve_deps(dependencies: list[str]) -> list[str]:
    deps: list[str] = []
    for d in list(dependencies or []) + list(BASE_DEPS):
        coord = (d or "").strip().strip('"').strip("'")
        if not coord or ":" not in coord or coord in deps:
            continue
        parts = coord.split(":")
        if len(parts) == 2:
            ver = KNOWN_DEP_VERSIONS.get(coord)
            if not ver:
                continue                      # unversioned unknown → base covers
            coord = f"{coord}:{ver}"
        deps.append(coord)
    if any(":room-" in d for d in deps) and \
            not any("room-compiler" in d for d in deps):
        deps.append(f"androidx.room:room-compiler:{ROOM_VERSION}")
    return deps


def scaffold_project(project_dir: str | os.PathLike,
                     dependencies: Optional[list[str]] = None,
                     app_name: Optional[str] = None,
                     entry_activity: Optional[str] = None) -> str:
    """Write the Gradle skeleton an LLM's Kotlin/XML sources need, with the app
    package detected from those sources. Returns a short human note.

    Writes (idempotent, always overwritten — the scaffold owns these):
      settings.gradle.kts, build.gradle.kts, gradle.properties,
      local.properties (when an SDK is found), app/build.gradle.kts,
      app/src/main/AndroidManifest.xml, res/values/strings.xml,
      and any @string/@color the layouts reference but never declared.
    """
    project_dir = Path(project_dir)
    project_dir.mkdir(parents=True, exist_ok=True)
    app_name = sanitize_app_name(app_name or project_dir.name)

    layout_note = normalize_layout(project_dir)
    reloc_note = relocate_stray_sources(project_dir)
    note_prefix = "[AndroidScaffold] " + layout_note
    if reloc_note:
        note_prefix += reloc_note + "; "
    pkg, activity_cls = detect_package(project_dir, entry_activity or "")
    deps = _resolve_deps(dependencies or [])
    has_room = any(":room-" in d for d in deps)

    deps_block = "\n".join(f'    implementation("{d}")' for d in deps)
    kapt_line = (f'\n    kapt("androidx.room:room-compiler:{ROOM_VERSION}")'
                 if has_room else "")
    kapt_plugin = '\n    id("org.jetbrains.kotlin.kapt")' if has_room else ""
    kapt_root = ('\n    id("org.jetbrains.kotlin.kapt") '
                 f'version "{KOTLIN_VERSION}" apply false' if has_room else "")

    (project_dir / "settings.gradle.kts").write_text(
        "pluginManagement {\n"
        "    repositories { google(); mavenCentral(); gradlePluginPortal() }\n"
        "}\n"
        "dependencyResolutionManagement {\n"
        "    repositoriesMode.set(RepositoriesMode.FAIL_ON_PROJECT_REPOS)\n"
        "    repositories { google(); mavenCentral() }\n"
        "}\n"
        f'rootProject.name = "{project_dir.name}"\n'
        'include(":app")\n', encoding="utf-8")

    (project_dir / "build.gradle.kts").write_text(
        "plugins {\n"
        f'    id("com.android.application") version "{AGP_VERSION}" apply false\n'
        f'    id("org.jetbrains.kotlin.android") version "{KOTLIN_VERSION}" apply false'
        f"{kapt_root}\n"
        "}\n", encoding="utf-8")

    (project_dir / "gradle.properties").write_text(
        "org.gradle.jvmargs=-Xmx2048m -Dfile.encoding=UTF-8\n"
        "android.useAndroidX=true\n"
        "android.nonTransitiveRClass=true\n"
        "kotlin.code.style=official\n", encoding="utf-8")

    sdk = sdk_path()
    if sdk:
        (project_dir / "local.properties").write_text(
            f"sdk.dir={sdk}\n", encoding="utf-8")

    app_dir = project_dir / "app"
    app_dir.mkdir(parents=True, exist_ok=True)
    (app_dir / "build.gradle.kts").write_text(
        "plugins {\n"
        '    id("com.android.application")\n'
        '    id("org.jetbrains.kotlin.android")'
        f"{kapt_plugin}\n"
        "}\n\n"
        "android {\n"
        f'    namespace = "{pkg}"\n'
        f"    compileSdk = {COMPILE_SDK}\n\n"
        "    defaultConfig {\n"
        f'        applicationId = "{pkg}"\n'
        f"        minSdk = {MIN_SDK}\n"
        f"        targetSdk = {TARGET_SDK}\n"
        "        versionCode = 1\n"
        '        versionName = "1.0"\n'
        "    }\n\n"
        "    compileOptions {\n"
        "        sourceCompatibility = JavaVersion.VERSION_17\n"
        "        targetCompatibility = JavaVersion.VERSION_17\n"
        "    }\n"
        '    kotlinOptions { jvmTarget = "17" }\n'
        "    buildFeatures { viewBinding = true }\n"
        "}\n\n"
        "// kapt drags in com.intellij:annotations:12.0 — a DIFFERENT module\n"
        "// coordinate than org.jetbrains:annotations, so force alone cannot\n"
        "// dedupe it; exclude it (nothing at runtime needs IntelliJ annotations).\n"
        "configurations.all {\n"
        '    resolutionStrategy.force("org.jetbrains:annotations:23.0.0")\n'
        '    exclude(group = "com.intellij", module = "annotations")\n'
        "}\n\n"
        "dependencies {\n"
        f"{deps_block}{kapt_line}\n"
        "}\n", encoding="utf-8")

    # Manifest: launcher activity + any other activity/receiver the plan wrote.
    # Extra ACTIVITIES matter now that the plan builds multi-screen, interactive
    # apps: an activity missing from the manifest compiles fine but crashes the
    # instant the user taps a control that starts it. Names are made fully
    # qualified from the source path so sub-package classes resolve too.
    java_root = app_dir / "src/main/java"
    extra_activities = []
    for src in list(java_root.glob("**/*.kt")) + list(java_root.glob("**/*.java")):
        stem = src.stem
        if stem == activity_cls or not stem.endswith("Activity"):
            continue
        rel = src.with_suffix("").relative_to(java_root)
        extra_activities.append(".".join(rel.parts))
    extra_activities = sorted(set(extra_activities))
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
    launcher = launcher_name(project_dir, pkg, activity_cls)
    manifest = f"""<?xml version="1.0" encoding="utf-8"?>
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
"""
    mf = app_dir / "src/main/AndroidManifest.xml"
    mf.parent.mkdir(parents=True, exist_ok=True)
    mf.write_text(manifest, encoding="utf-8")

    res_values = app_dir / "src/main/res/values"
    res_values.mkdir(parents=True, exist_ok=True)
    (res_values / "strings.xml").write_text(
        '<?xml version="1.0" encoding="utf-8"?>\n'
        "<resources>\n"
        f'    <string name="app_name">{app_name}</string>\n'
        "</resources>\n", encoding="utf-8")

    auto_n = _auto_define_resources(app_dir / "src/main/res", res_values)

    # The planner names ids in the XML and the Kotlin independently — realign
    # them here (deterministic, pre-build) so "Unresolved reference: <id>" never
    # reaches the fix loop.
    id_note = reconcile_view_ids(project_dir)

    note = note_prefix + f"package {pkg}, activity {launcher}"
    if auto_n:
        note += f" | auto-defined {auto_n} missing resource(s)"
    if id_note:
        note += " | " + id_note
    if not sdk:
        note += (" | ⚠ no Android SDK found — run "
                 "scripts/android_bootstrap.sh once, then rebuild")
    if not gradle_bin():
        note += " | ⚠ no gradle — install via scripts/android_bootstrap.sh"
    return note


def _auto_define_resources(res_root: Path, res_values: Path) -> int:
    """Auto-declare @string/@color the layouts reference but never defined (a
    missing one fails resource linking — a deterministic fix, no LLM round)."""
    strings_defined = {"app_name"}
    colors_defined: set = set()
    defined = res_values / "strings.xml"
    if defined.exists():
        strings_defined |= set(re.findall(
            r'<string name="([\w]+)"', defined.read_text(encoding="utf-8")))
    colors_file = res_values / "colors.xml"
    if colors_file.exists():
        colors_defined |= set(re.findall(
            r'<color name="([\w]+)"', colors_file.read_text(encoding="utf-8")))
    wanted_strings: set = set()
    wanted_colors: set = set()
    for xml in res_root.glob("**/*.xml"):
        if res_values in xml.parents:
            continue
        try:
            txt = xml.read_text(encoding="utf-8")
        except Exception:
            continue
        wanted_strings |= set(re.findall(r"@string/([\w]+)", txt))
        wanted_colors |= set(re.findall(r"@color/([\w]+)", txt))
    add_s = "".join(
        f'    <string name="{n}">'
        f'{n.replace("_", " ").strip().capitalize()}</string>\n'
        for n in sorted(wanted_strings - strings_defined))
    add_c = "".join(f'    <color name="{n}">#6750A4</color>\n'
                    for n in sorted(wanted_colors - colors_defined))
    if add_c:
        if colors_file.exists():
            colors_file.write_text(
                colors_file.read_text(encoding="utf-8")
                .replace("</resources>", add_c + "</resources>"),
                encoding="utf-8")
        else:
            colors_file.write_text(
                '<?xml version="1.0" encoding="utf-8"?>\n'
                "<resources>\n" + add_c + "</resources>\n", encoding="utf-8")
    if add_s:
        defined.write_text(
            defined.read_text(encoding="utf-8")
            .replace("</resources>", add_s + "</resources>"), encoding="utf-8")
    return len(wanted_strings - strings_defined) + \
        len(wanted_colors - colors_defined)


# ── view-id reconciliation ───────────────────────────────────────────────────

_ID_ATTR = re.compile(r'android:id\s*=\s*"@\+?id/([\w]+)"')
_ELEMENT = re.compile(r"<([A-Za-z][\w.]*)((?:[^>\"']|\"[^\"]*\"|'[^']*')*?)/?>")
_SOURCE_ID = re.compile(r"(?<![\w.])R\.id\.([A-Za-z_]\w*)")


def _layout_views(res_root: Path) -> dict[str, list[tuple[str, str]]]:
    """{layout_stem: [(widget_type, id_or_'')]} for every res/layout/*.xml."""
    layouts: dict[str, list[tuple[str, str]]] = {}
    for xml in sorted((res_root / "layout").glob("*.xml")):
        try:
            text = xml.read_text(encoding="utf-8")
        except OSError:
            continue
        views: list[tuple[str, str]] = []
        for m in _ELEMENT.finditer(text):
            tag, attrs = m.group(1), m.group(2)
            if tag.startswith("?") or tag.lower().startswith("xml"):
                continue
            idm = _ID_ATTR.search(attrs)
            views.append((tag.split(".")[-1], idm.group(1) if idm else ""))
        layouts[xml.stem] = views
    return layouts


def _declared_type(text: str, ref: str) -> str:
    """The widget type a `R.id.<name>` is assigned to, or '' when unclear —
    `val x: ImageButton = findViewById(...)` or `findViewById<RecyclerView>(...)`."""
    idx = text.find(ref)
    while idx != -1:
        start = text.rfind("\n", 0, idx) + 1
        end = text.find("\n", idx)
        line = text[start:(end if end != -1 else len(text))]
        m = (re.search(r":\s*([A-Z][\w.]*)\s*=", line)
             or re.search(r"findViewById\s*<\s*([A-Z][\w.]*)\s*>", line)
             or re.search(r"\bas\s+([A-Z][\w.]*)\s*$", line.strip()))
        if m:
            return m.group(1).split(".")[-1]
        idx = text.find(ref, idx + 1)
    return ""


def reconcile_view_ids(project_dir: str | os.PathLike) -> str:
    """Point every `R.id.x` at an id the layouts actually declare.

    The LLM writes the Kotlin and the XML in separate calls, so their id names
    drift (`findViewById(R.id.image_button_settings)` against `@+id/btnSettings`)
    and Kotlin fails with "Unresolved reference" — an error a code fixer cannot
    repair by rewriting the .kt alone, because it neither sees nor may edit the
    layout, so it invents a new id every round (observed 2026-10-17: five edits,
    five invented ids, no build).

    Deterministic and conservative: a reference is repointed ONLY when a layout
    holds exactly ONE still-unreferenced view of the same widget type, so the
    target is unambiguous. Ambiguous references are left for the model.
    """
    project_dir = Path(project_dir)
    java_root = project_dir / "app/src/main/java"
    res_root = project_dir / "app/src/main/res"
    if not java_root.exists() or not res_root.exists():
        return ""

    layouts = _layout_views(res_root)
    defined = {i for views in layouts.values() for _, i in views if i}
    if not defined:
        return ""

    texts: dict[str, str] = {}
    refs: set[str] = set()
    for p in (list(java_root.glob("**/*.kt")) +
              list(java_root.glob("**/*.java"))):
        try:
            texts[str(p)] = p.read_text(encoding="utf-8")
        except OSError:
            continue
        refs |= set(_SOURCE_ID.findall(texts[str(p)]))

    repaired: list[str] = []
    for path, text in texts.items():
        missing = sorted(set(_SOURCE_ID.findall(text)) - defined)
        if not missing:
            continue
        preferred = re.search(r"setContentView\s*\(\s*R\.layout\.([\w]+)", text)
        pref = preferred.group(1) if preferred else ""
        order = ([pref] if pref else []) + [n for n in layouts if n != pref]
        new_text = text
        for miss in missing:
            wanted = _declared_type(new_text, f"R.id.{miss}")
            if not wanted:
                continue
            cands: list[str] = []
            for name in order:
                for vtype, vid in layouts.get(name, []):
                    if vid and vid not in refs and vtype == wanted \
                            and vid not in cands:
                        cands.append(vid)
            if len(cands) == 1:
                new_text = re.sub(
                    r"(?<![\w.])R\.id\." + re.escape(miss) + r"\b",
                    f"R.id.{cands[0]}", new_text)
                refs.add(cands[0])
                repaired.append(f"{miss}→{cands[0]} ({Path(path).name})")
        if new_text != text:
            Path(path).write_text(new_text, encoding="utf-8")

    if not repaired:
        return ""
    return (f"repaired {len(repaired)} view-id reference(s): "
            + ", ".join(repaired))


# ── build ────────────────────────────────────────────────────────────────────
def build_argv(project_dir: str | os.PathLike) -> Optional[list[str]]:
    """The gradle argv that builds a debug APK, or None when no gradle exists.
    Prefers the project's own ./gradlew wrapper when present."""
    project_dir = Path(project_dir)
    wrapper = project_dir / "gradlew"
    if wrapper.exists():
        return [str(wrapper), "assembleDebug", "--no-daemon", "-q"]
    gradle = gradle_bin()
    if gradle:
        return [gradle, "assembleDebug", "--no-daemon", "-q"]
    return None


def gradle_argv_ok(argv: list[str]) -> tuple[bool, str]:
    """Allow only the build subcommands/flags the agent legitimately needs.
    `gradle` can run arbitrary build scripts, so nothing beyond assemble/clean
    and a few non-executing flags is permitted."""
    if not argv:
        return False, "empty gradle command"
    args = [str(a) for a in argv[1:]]
    if not args:
        return False, "gradle needs a task (assembleDebug)"
    for a in args:
        low = a.lower()
        if low in GRADLE_ALLOWED_TASKS or a in ("--no-daemon", "--offline"):
            continue
        if a.startswith("--console=") or (a.startswith("--console")
                                          and low != "--console"):
            continue
        if low in GRADLE_ALLOWED_FLAGS:
            continue
        if a.startswith("-D") or a.startswith("--project-dir="):
            continue
        return False, (f"gradle argument '{a}' is not on the build allowlist "
                       f"(assembleDebug/assembleRelease/clean only)")
    if not any(a.lower() in ("assembledebug", "assemblerelease", "build")
               for a in args) and not any(
                   a.lower() in ("clean", "tasks", "help", "--version", "-v")
                   for a in args):
        return False, "gradle task must be assembleDebug/assembleRelease"
    return True, ""


# ── desktop export ───────────────────────────────────────────────────────────
def export_to_desktop(project_dir: str | os.PathLike,
                      apk: Optional[str | os.PathLike],
                      app_name: Optional[str] = None) -> dict:
    """Save the project + APK on the Desktop.

    The project folder already lives on the Desktop (`~/Desktop/<App>`), so this
    copies the built APK to `<project>/<App>.apk` and to `~/Desktop/<App>.apk`,
    and reports both. Never raises; missing APK is reported honestly."""
    project_dir = Path(project_dir)
    app_name = sanitize_app_name(app_name or project_dir.name)
    out: dict = {"project_dir": str(project_dir), "apk": None,
                 "apk_in_project": None, "apk_on_desktop": None,
                 "copied": False, "error": ""}
    if not apk:
        out["error"] = "no APK was produced to save"
        return out
    apk = Path(apk)
    if not apk.is_file():
        out["error"] = f"APK not found at {apk}"
        return out
    out["apk"] = str(apk)
    try:
        project_copy = project_dir / f"{app_name}.apk"
        if apk.resolve() != project_copy.resolve():
            shutil.copy2(apk, project_copy)
        out["apk_in_project"] = str(project_copy)
        desktop_copy = desktop_dir() / f"{app_name}.apk"
        if apk.resolve() != desktop_copy.resolve():
            shutil.copy2(apk, desktop_copy)
        out["apk_on_desktop"] = str(desktop_copy)
        out["copied"] = True
    except Exception as exc:                      # never break the agent
        out["error"] = f"could not save the APK to the Desktop: {exc}"
    return out
