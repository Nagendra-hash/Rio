"""
MARK LIII — one-time setup.

Installs the Python dependencies for THIS operating system only: the OS-specific
packages in requirements.txt carry `sys_platform` markers, so a macOS or Linux
user never pulls Windows-only libraries (and vice-versa). Then it fetches the
Playwright browsers needed for web automation (current-OS builds only).

The optional local wake word ("Hey Jarvis") is NOT installed here — it's a
one-click, opt-in download from ⚙ → WAKE WORD inside the app.
"""
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

OS = platform.system()  # "Windows" | "Darwin" | "Linux"


def _run(label: str, args: list[str]) -> None:
    print(f"\n▶ {label}")
    subprocess.run(args, check=True)


def _ensure_pip() -> None:
    """Guarantee `python -m pip` works in the current interpreter.

    Environments built by uv (`uv venv`) do not seed pip by default, so a bare
    `python -m pip` fails with "No module named pip". Anything that later shells
    out to `sys.executable -m pip` (e.g. the wake-word one-click installer in
    core/wake_word.py) would fail the same way. bootstrap pip via ensurepip when
    it is missing; if ensurepip is unavailable too (some distro builds strip it),
    fall back to the uv shim (uv pip) when uv is on PATH.
    """
    probe = subprocess.run([sys.executable, "-m", "pip", "--version"],
                           capture_output=True)
    if probe.returncode == 0:
        return   # pip already present — nothing to do

    print("\n▶ Bootstrapping pip (missing in this environment)…")
    r = subprocess.run([sys.executable, "-m", "ensurepip", "--upgrade"])
    if r.returncode == 0:
        return

    # ensurepip unavailable (distro-stripped Python) — try the uv pip shim.
    if shutil.which("uv"):
        venv = os.environ.get("VIRTUAL_ENV")
        if venv:
            shim = Path(venv) / "bin" / "pip"
            shim.write_text(
                "#!/bin/sh\n"
                "exec uv pip --python \"$VIRTUAL_ENV/bin/python\" \"$@\"\n"
            )
            shim.chmod(0o755)
            print("   pip was missing — wrote a uv-backed pip shim into the venv.")
            return

    print("\n⚠️  Could not bootstrap pip automatically. Wake-word install and any"
          "\n   'pip install <module>' fallbacks will need one of:\n"
          "       python -m ensurepip --upgrade\n"
          "       uv pip install …\n"
          "       python -m venv --clear <dir>   (recreate with seeded pip)")


def main() -> None:
    print(f"⚙  MARK LIII setup — detected OS: {OS or 'unknown'}")

    _ensure_pip()

    # requirements.txt filters OS-specific extras by itself via pip markers.
    _run("Installing Python dependencies (OS-specific extras auto-filtered)…",
         [sys.executable, "-m", "pip", "install", "-r", "requirements.txt"])

    # Chromium covers Chrome/Edge/Opera/Brave/Vivaldi; Firefox for Firefox.
    # (Safari automation additionally needs: python -m playwright install webkit)
    _run("Installing Playwright browsers (chromium + firefox)…",
         [sys.executable, "-m", "playwright", "install", "chromium", "firefox"])

    # ── OS-specific post-install notes ────────────────────────────────────────
    if OS == "Windows":
        try:
            import win32com.client  # noqa: F401
        except ImportError:
            postinstall = Path(sys.executable).parent / "Scripts" / "pywin32_postinstall.py"
            print(
                "\n⚠️  pywin32 did not register correctly — desktop-shortcut "
                "creation will use a slower fallback. To fix it, run:\n"
                f'    "{sys.executable}" -m pip install --force-reinstall pywin32\n'
                f'    "{sys.executable}" "{postinstall}" -install'
            )
    elif OS == "Linux":
        print(
            "\nℹ️  Linux note — a few voice-controlled OS actions shell out to "
            "native tools. Install the ones you'll use via your package manager:\n"
            "    • volume      → pulseaudio-utils   (pactl)\n"
            "    • brightness  → brightnessctl\n"
            "    • reminders   → systemd (systemd-run) or 'at'\n"
            "    • open URLs   → xdg-utils          (xdg-open)"
        )
    elif OS == "Darwin":
        print(
            "\nℹ️  macOS note — volume, brightness and reminders use the built-in "
            "'osascript' / LaunchAgents, so no extra tools are required.\n"
            "    For Safari automation only: python -m playwright install webkit"
        )

    print("\n✅ Setup complete!")
    print("   1) Launch it:  python main.py")
    print("   2) Paste your free Gemini API key when the setup screen appears.")
    print("   3) (Optional) Enable 'Hey Jarvis' from ⚙ → WAKE WORD.")


if __name__ == "__main__":
    main()
