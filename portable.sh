#!/bin/bash
# portable.sh — run JARVIS straight from a pendrive on any Linux/macOS machine.
#
# Modes, decided by what the drive contains:
#   • jarvis-portable/python/bin/python3 exists → relocatable runtime prepared
#     by "python3 scripts/build_portable.py --linux" on a machine that has
#     internet (see PORTABLE.md). Dependencies live in jarvis-portable/
#     site-packages/. No internet, no host Python needed at all.
#   • jarvis-portable/venv exists → venv created on this drive by an earlier
#     first run of this script (or the legacy --linux-venv build).
#   • otherwise → setup mode: creates a venv ON THE DRIVE from the host's
#     python3 (3.11+ required) and installs requirements. Needs internet ONCE.
#
# Everything writable (config/, memory/, the conversation journal) lives in
# jarvis-home/ on the drive via JARVIS_HOME — nothing is written to the host
# outside /tmp. Plug in, run, unplug when done.
#
# NB: written for bash 3.2 (macOS ships no newer) — no sort -V, no readlink -f,
# no associative arrays.
set -euo pipefail

# ── Locate the app tree without readlink -f (absent on macOS) ───────────────
# Walk up from $0 through symlinks with cd -P; ends at the physical directory
# containing this script. Falls back to PWD if the walk cannot start.
APP_DIR=""
TARGET="$0"
while [ -L "$TARGET" ]; do
    LINK="$(ls -ld -- "$TARGET" 2>/dev/null | sed 's/^.* -> //')"
    case "$LINK" in
        /*) TARGET="$LINK" ;;
        *)  TARGET="$(dirname -- "$TARGET")/$LINK" ;;
    esac
done
APP_DIR="$(cd -P "$(dirname -- "$TARGET")" 2>/dev/null && pwd)" || APP_DIR="$PWD"
if [ ! -f "$APP_DIR/main.py" ]; then
    echo "[portable] ERROR: cannot locate the JARVIS folder (run this from the" >&2
    echo "[portable] drive, or fix the symlink chain). Looked at: $APP_DIR" >&2
    exit 1
fi

# ── Writable home on the drive ──────────────────────────────────────────────
export JARVIS_HOME="$APP_DIR/jarvis-home"
mkdir -p "$JARVIS_HOME" || {
    echo "[portable] ERROR: cannot create $JARVIS_HOME — is the drive read-only?" >&2
    exit 1
}

PORTABLE="$APP_DIR/jarvis-portable"
PY=""

# ── Prepared relocatable runtime (build_portable.py --linux) ─────────────────
if [ -x "$PORTABLE/python/bin/python3" ]; then
    PY="$PORTABLE/python/bin/python3"
    # Dependencies were installed --target into the sibling site-packages/
    # folder; the relocatable runtime does not know about it by itself.
    if [ -d "$PORTABLE/site-packages" ]; then
        export PYTHONPATH="$PORTABLE/site-packages${PYTHONPATH:+:$PYTHONPATH}"
    fi
# ── Venv created on the drive by a previous first run ────────────────────────
elif [ -x "$PORTABLE/venv/bin/python" ]; then
    PY="$PORTABLE/venv/bin/python"
else
    echo "[portable] First run on this drive — one-time setup (needs internet)."
    echo "[portable] Afterwards the drive runs offline on any Linux/Mac machine."
    echo
    HOST_PY="$(command -v python3 || true)"
    if [ -z "$HOST_PY" ]; then
        echo "[portable] ERROR: no python3 on this machine and no prepared runtime" >&2
        echo "[portable] on the drive. Build the bundle with" >&2
        echo "[portable] 'python3 scripts/build_portable.py --linux' on a machine" >&2
        echo "[portable] that has Python, and copy jarvis-portable/ over." >&2
        exit 1
    fi
    VM="$("$HOST_PY" -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
    VMAJOR="${VM%%.*}"
    VMINOR="${VM#*.}"
    if [ "$VMAJOR" -lt 3 ] || { [ "$VMAJOR" -eq 3 ] && [ "$VMINOR" -lt 11 ]; }; then
        echo "[portable] ERROR: host Python is $VM; 3.11+ is required." >&2
        echo "[portable] Use scripts/build_portable.py --linux on a newer machine" >&2
        echo "[portable] instead (the resulting runtime needs no Python at all)." >&2
        exit 1
    fi
    mkdir -p "$PORTABLE"
    echo "[portable] Creating venv on the drive (host Python $VM)..."
    "$HOST_PY" -m venv "$PORTABLE/venv"
    PY="$PORTABLE/venv/bin/python"
    echo "[portable] Installing dependencies (one time — the slow part)..."
    "$PY" -m pip install --upgrade pip
    "$PY" -m pip install -r "$APP_DIR/requirements.txt"
    : > "$PORTABLE/.deps-ok"
fi

# ── Dependencies complete? A missing marker means an interrupted install. ────
if [ ! -f "$PORTABLE/.deps-ok" ]; then
    echo "[portable] Dependencies incomplete — finishing install (needs internet)..."
    "$PY" -m pip install -r "$APP_DIR/requirements.txt"
    : > "$PORTABLE/.deps-ok"
fi

echo "[portable] JARVIS_HOME=$JARVIS_HOME"
echo "[portable] Starting JARVIS..."
export PYTHONUNBUFFERED=1
exec "$PY" "$APP_DIR/main.py" "$@"
