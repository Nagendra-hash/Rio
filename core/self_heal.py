"""
self_heal.py — JARVIS fixes its own missing dependencies.

Goal: the user gives a task; if an action/plugin dies because a library or
system binary is missing, JARVIS installs it itself and retries — no
intervention, no dead end. Three layers:

  1. pip    — import-name → PyPI-name mapping (PIL→Pillow, cv2→opencv-python,
              fitz→PyMuPDF, …). Unknown names are asked to the LLM (through the
              llm_router failover chain, so it works even mid-outage).
  2. binaries — apt package per known executable (adb→android-tools-adb,
              scrcpy→scrcpy, ffmpeg→ffmpeg, …). System packages need root:
              we NEVER run sudo blindly — a GUI pkexec prompt is raised so the
              user sees and approves the install with their password.
  3. policy — thread lock + in-memory attempt log: one attempt per package per
              cooldown window; a post-install importability probe confirms the
              package actually works before we report success.

Nothing here ever raises into the caller: every entry point returns a
(diag, ok) tuple. Call sites:
  - core/action_loader.py: on module import failure, heal + re-import once.
  - ActionRegistry.run(): on a ModuleNotFoundError/ImportError crash, heal
    + re-import the action module + retry the call once.
"""
from __future__ import annotations

import importlib
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

_BASE = Path(__file__).resolve().parent.parent
# JARVIS_HOME (pendrive mode): the key file read at heal time lives in the
# relocatable home — on a fresh host PC the drive is the only place it exists.
import os as _os
_home = _os.environ.get("JARVIS_HOME", "").strip()
if _home:
    _BASE = Path(_home).expanduser().resolve()

# ── import name → PyPI distribution name (the ones that differ) ──────────────
_IMPORT_TO_PYPI: dict[str, str] = {
    "PIL": "Pillow",
    "cv2": "opencv-python",
    "fitz": "PyMuPDF",
    "sklearn": "scikit-learn",
    "yaml": "PyYAML",
    "Crypto": "pycryptodome",
    "serial": "pyserial",
    "git": "GitPython",
    "dateutil": "python-dateutil",
    "dotenv": "python-dotenv",
    "attr": "attrs",
    "bs4": "beautifulsoup4",
    "docx": "python-docx",
    "pptx": "python-pptx",
    "jwt": "PyJWT",
    "dns": "dnspython",
    "socketio": "python-socketio",
    "github": "PyGithub",
    "markdown": "markdown",
    "psutil": "psutil",
    "socks": "PySocks",
    "pygame": "pygame",
    "speech_recognition": "SpeechRecognition",
    "pyttsx3": "pyttsx3",
    "qrcode": "qrcode",
    "pyautogui": "pyautogui",
    "pynput": "pynput",
    "mss": "mss",
}

# import names that must NEVER be auto-installed even if a traceback names them
# (stdlib modules people misspell, or names that shadow JARVIS' own packages).
_DENYLIST = {"os", "sys", "time", "json", "types", "io", "re", "math", "subprocess",
             "threading", "asyncio", "logging", "utils", "core", "actions", "plugins",
             "memory", "config", "dashboard", "tests", "main"}

# ── executable → apt package (Linux) ─────────────────────────────────────────
_BIN_TO_APT = {
    "adb": "android-tools-adb",
    "scrcpy": "scrcpy",
    "ffmpeg": "ffmpeg",
    "tesseract": "tesseract-ocr",
    "pandoc": "pandoc",
    "xdotool": "xdotool",
    "wmctrl": "wmctrl",
    "zenity": "zenity",
}

_install_lock = threading.Lock()
_attempted: dict[str, float] = {}          # key → last attempt time
_COOLDOWN_S = 900                          # don't retry a failed pkg for 15 min


def _log(msg: str) -> None:
    print(f"[SelfHeal] {msg}")


# ── pip layer ────────────────────────────────────────────────────────────────

def _ask_llm_for_pypi_name(import_name: str) -> str | None:
    """Unknown import name → ask the LLM (via llm_router failover) for the PyPI
    distribution name. Returns None on any failure — never raises."""
    try:
        from google import genai
        from google.genai import types
        from core.llm_router import install_router  # idempotent; enables failover
        import json as _json
        key = (_json.loads((_BASE / "config" / "api_keys.json").read_text())
               .get("gemini_api_key") or "").strip()
        if not key:
            return None
        client = genai.Client(api_key=key)
        resp = client.models.generate_content(
            model="gemini-flash-lite-latest",
            contents=(
                f'A Python module named "{import_name}" failed to import. '
                "Reply with ONLY the PyPI package name that provides it "
                "(lowercase, no spaces), or NONE if you are not confident. "
                "Examples: PIL→Pillow, cv2→opencv-python, yaml→PyYAML, "
                "sklearn→scikit-learn, bs4→beautifulsoup4."
            ),
            config=types.GenerateContentConfig(
                system_instruction="You convert Python import names to PyPI "
                                   "distribution names. Answer with one word.",
                max_output_tokens=2048,
            ),
        )
        ans = (resp.text or "").strip().strip("`\"' .")
        if not ans or ans.upper() == "NONE" or not re.fullmatch(r"[A-Za-z0-9_.\-]{2,60}", ans):
            return None
        return ans
    except Exception as e:
        _log(f"LLM mapping lookup failed for '{import_name}': {e}")
        return None


def pypi_name_for(import_name: str, use_llm: bool = True) -> str | None:
    import_name = (import_name or "").strip()
    if not import_name or import_name.lower() in _DENYLIST:
        return None
    if import_name in _IMPORT_TO_PYPI:
        return _IMPORT_TO_PYPI[import_name]
    if use_llm:
        return _ask_llm_for_pypi_name(import_name)
    return import_name  # best guess: same name


def _can_import(import_name: str) -> bool:
    try:
        importlib.import_module(import_name)
        return True
    except Exception:
        return False


def _pip_install(pkg: str, import_name: str) -> bool:
    """Install `pkg` into the RUNNING interpreter (venv-aware via sys.executable)
    and verify `import_name` (or the dist name as a module) becomes importable."""
    try:
        r = subprocess.run(
            [sys.executable, "-m", "pip", "install", "--quiet", pkg],
            capture_output=True, text=True, timeout=300,
        )
        if r.returncode != 0:
            _log(f"pip install {pkg} failed: {(r.stderr or r.stdout or '')[-200:]}")
            return False
        for probe in (import_name, pkg.replace("-", "_")):
            try:
                importlib.import_module(probe)
                return True
            except Exception:
                continue
        _log(f"pip install {pkg} succeeded but '{import_name}' still won't import")
        return False
    except Exception as e:
        _log(f"pip install {pkg} error: {e}")
        return False


def ensure_python(import_name: str) -> tuple[str, bool]:
    """Ensure `import_name` is importable; install if missing.
    Returns (diagnostic, ok). Never raises."""
    import_name = (import_name or "").strip()
    if not import_name:
        return "empty module name", False
    if _can_import(import_name):
        return f"'{import_name}' already importable", True
    if import_name.lower() in _DENYLIST:
        return f"'{import_name}' is on the denylist — not auto-installing", False

    with _install_lock:
        now = time.time()
        if now - _attempted.get(f"py:{import_name}", 0) < _COOLDOWN_S:
            return f"'{import_name}' install attempted recently — cooldown", False
        _attempted[f"py:{import_name}"] = now

        pkg = pypi_name_for(import_name, use_llm=False)
        if pkg and _pip_install(pkg, import_name):
            return f"installed '{pkg}' from alias table", True
        pkg2 = pypi_name_for(import_name, use_llm=True)
        if pkg2 and pkg2 != pkg and _pip_install(pkg2, import_name):
            return f"installed '{pkg2}' (LLM-mapped)", True
        return f"could not install a PyPI package providing '{import_name}'", False


# ── system binary layer (Linux) ──────────────────────────────────────────────

def _have(binary: str) -> bool:
    return shutil.which(binary) is not None


def _pkexec_install(apt_pkg: str) -> bool:
    """Install an apt package with a GUI polkit prompt (user sees and approves
    with their password). Never runs an invisible privileged install."""
    try:
        r = subprocess.run(
            ["pkexec", "apt-get", "install", "-y", apt_pkg],
            capture_output=True, text=True, timeout=600,
        )
        return r.returncode == 0
    except Exception as e:
        _log(f"pkexec install of {apt_pkg} failed: {e}")
        return False


def ensure_binary(binary: str) -> tuple[str, bool]:
    """Ensure a system executable exists; offer GUI-approved apt install.
    Returns (diagnostic, ok). Never raises."""
    binary = (binary or "").strip()
    if not binary:
        return "empty binary name", False
    if _have(binary):
        return f"'{binary}' present", True

    with _install_lock:
        now = time.time()
        if now - _attempted.get(f"bin:{binary}", 0) < _COOLDOWN_S:
            return f"'{binary}' install attempted recently — cooldown", False
        _attempted[f"bin:{binary}"] = now

        apt_pkg = _BIN_TO_APT.get(binary)
        if apt_pkg and sys.platform.startswith("linux"):
            _log(f"'{binary}' missing — raising polkit prompt to install '{apt_pkg}' "
                 "(enter your password in the dialog)")
            if _pkexec_install(apt_pkg):
                return f"installed system package '{apt_pkg}'", _have(binary)
            return (f"polkit install of '{apt_pkg}' was not approved or failed — "
                    f"run: sudo apt install {apt_pkg}"), False
        return (f"'{binary}' not found and no known package for it — "
                f"install it manually (apt/npm/brew)"), False


# ── error parsing ────────────────────────────────────────────────────────────

_MOD_RE = re.compile(r"No module named (?:['\"])([A-Za-z0-9_.\-]+)(?:['\"])")
_BIN_RE = re.compile(
    r"(?:FileNotFoundError|command not found|not found)[^\n]*?\b"
    r"(adb|scrcpy|ffmpeg|tesseract|pandoc|xdotool|wmctrl|zenity)\b",
    re.IGNORECASE,
)


def heal_from_error(error_text: str) -> tuple[str, bool]:
    """Parse an error string, heal whatever it names. (diag, ok). Never raises."""
    if not error_text:
        return "empty error", False
    m = _MOD_RE.search(error_text)
    if m:
        mod = m.group(1).split(".")[0]
        return ensure_python(mod)
    m = _BIN_RE.search(error_text)
    if m:
        return ensure_binary(m.group(1).lower())
    return "no known-missing dependency found in error", False
