#!/bin/bash
# ─────────────────────────────────────────────────────────────────────────────
# run.sh — launch Mark-LIII with the project's virtualenv Python.
#
# The app uses asyncio.TaskGroup and exception groups (Python 3.11+); the
# system python3 on Ubuntu 22.04 is 3.10 and crashes on startup. The project's
# .venv carries Python 3.12, so always launch through this script (or
# `.venv/bin/python main.py` directly).
#
# Usage:   ./run.sh            (any extra args are passed to main.py)
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

# Always run from the project root, no matter where the script was invoked from
cd "$(dirname "$(readlink -f "$0")")"
ROOT="$PWD"

PY="$ROOT/.venv/bin/python"

# Heal a venv that was moved/renamed with the project: pip and `activate` bake
# the absolute interpreter path into every console script and into VIRTUAL_ENV,
# so a stale prefix leaves `python` unresolvable on PATH. Repoint it in place.
if [ -f .venv/pyvenv.cfg ] && grep -q "home = " .venv/pyvenv.cfg; then
    STALE="$(grep -o "/home/[^/]*/Mark-[^/]*" .venv/bin/activate 2>/dev/null | head -1 || true)"
    if [ -n "$STALE" ] && [ "$STALE" != "$ROOT" ]; then
        echo "[run.sh] Repairing venv paths ($STALE → $ROOT)…"
        grep -rl "$STALE/" .venv/bin/ 2>/dev/null | xargs -r sed -i "s#$STALE/#$ROOT/#g"
    fi
fi

# Bootstrap: create the venv and install dependencies if it does not exist yet
if [ ! -x "$PY" ]; then
    echo "[run.sh] No .venv found — creating one and installing requirements…"
    python3 -m venv .venv
    .venv/bin/pip install --upgrade pip
    .venv/bin/pip install -r requirements.txt
fi

# Soft check: warn if the venv somehow holds an old interpreter (a 3.10 venv
# still works via the compat shim in main.py, but native 3.11+ is preferred)
VVER="$("$PY" -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
if [ "$(printf '%s\n3.11\n' "$VVER" | sort -V | head -1)" != "3.11" ]; then
    echo "[run.sh] ⚠️  venv Python is $VVER (3.11+ recommended; falling back to main.py's compat shim)"
fi

# Activate so child processes (installers, spawned tools) inherit venv PATH.
# `python` is resolved through the absolute $PY below, so a stale activate
# script can no longer break startup.
# shellcheck disable=SC1091
source .venv/bin/activate
export VIRTUAL_ENV="$ROOT/.venv"
export PATH="$ROOT/.venv/bin:$PATH"

# Line-buffered stdout: when output is redirected to /tmp/jarvis_live.log,
# Python block-buffers and status lines appear minutes late (or not at all
# before a crash). Unbuffered keeps the log and zombie_watch.sh honest.
export PYTHONUNBUFFERED=1

# Wayland input soft check: pyautogui clicks/typing are ignored on Wayland;
# JARVIS routes them through ydotool when present. Warn early if not.
if [ -n "${WAYLAND_DISPLAY:-}" ] && ! command -v ydotool >/dev/null 2>&1; then
    echo "[run.sh] ⚠️  Wayland session without ydotool — JARVIS cannot click or type."
    echo "[run.sh]    Fix:  sudo apt-get install ydotool   (then restart JARVIS)"
fi

# xcb GUI fallback: PyQt6's xcb platform plugin needs libxcb-cursor0, which
# Ubuntu 22.04 does not preinstall and installing it requires sudo. If the
# system copy is missing, point the loader at the deb-extracted copy kept in
# .system-libs/. Harmless no-op once `sudo apt-get install libxcb-cursor0` runs.
if [ ! -e /usr/lib/x86_64-linux-gnu/libxcb-cursor.so.0 ] && [ -d ".system-libs/usr/lib/x86_64-linux-gnu" ]; then
    export LD_LIBRARY_PATH="$PWD/.system-libs/usr/lib/x86_64-linux-gnu${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
fi

# ── RIO desktop window (default UI) ──────────────────────────────────────────
# JARVIS_RIO_WINDOW=1 (default): the window the user sees is RIO — served by
# the SAME dashboard/backend at /home, opened as a Chrome --app window by
# rio_window.py. main.py itself does NOT create the old PyQt HUD window in
# this mode (env gate checked inside main()); the opener thread inside main
# waits for the dashboard to come up, mints a pairing key and launches the
# window. Set JARVIS_RIO_WINDOW=0 to restore the legacy PyQt HUD behaviour.
if [ "${JARVIS_RIO_WINDOW:-1}" != "0" ]; then
    echo "[run.sh] RIO desktop window mode (set JARVIS_RIO_WINDOW=0 to restore the old HUD)"
fi

# Publish the pid for zombie_watch.sh (it samples /tmp/jarvis.pid + /tmp/jarvis_live.log).
# After `exec` the shell IS the python process, so $$ is the app's pid.
echo $$ > /tmp/jarvis.pid
echo "[run.sh] Starting JARVIS (venv Python $("$PY" --version 2>&1 | cut -d' ' -f2)) — pid $(cat /tmp/jarvis.pid)"

exec "$PY" main.py "$@"
