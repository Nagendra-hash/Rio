#!/bin/bash
# ─────────────────────────────────────────────────────────────────────────────
# jarvis_gui_wrapper.sh — single-instance launcher for the systemd user unit
#
# The unit (systemd/jarvis-gui.service) runs this instead of main.py so that
# "start with the desktop session" can never double-launch JARVIS next to a
# manually started copy — e.g. the UI's own AUTO-START button writing a
# ~/.config/autostart/jarvis.desktop entry, or you just running ./run.sh.
#
# Behavior: if a JARVIS process is already alive → exit 0 quietly (systemd
# sees success and will not restart-loop); otherwise start ./run.sh in the
# foreground so the unit tracks the real lifetime of the app.
#
# Env overrides:
#   JARVIS_FLOCK   lock file   (default /tmp/jarvis_gui.lock)
#   JARVIS_DEBUG   1 = log why the guard decided what it did to
#                  /tmp/jarvis_gui_wrapper.log
# ─────────────────────────────────────────────────────────────────────────────
set -u
cd "$(dirname "$(readlink -f "$0")")"

LOG=/tmp/jarvis_gui_wrapper.log
dbg() { [ "${JARVIS_DEBUG:-0}" = "1" ] && echo "$(date '+%F %T') $*" >> "$LOG"; }

# Another instance already running? (main.py is unambiguous; the grep -v
# filters this wrapper's own grep noise)
if pgrep -af "python.*main\.py" | grep -qv grep; then
    dbg "guard: JARVIS already running — exiting 0"
    exit 0
fi

# Flock: systemd can restart the unit faster than the Python process appears,
# so the pgrep race is real. Only the lock holder starts the app.
exec 9>"${JARVIS_FLOCK:-/tmp/jarvis_gui.lock}"
if ! flock -n 9; then
    dbg "guard: another start attempt holds the lock — exiting 0"
    exit 0
fi

# A lost lock from a crashed run would otherwise block every future start
[ -e "${JARVIS_FLOCK:-/tmp/jarvis_gui.lock}" ] || : > "${JARVIS_FLOCK:-/tmp/jarvis_gui.lock}"

dbg "guard: no instance found — launching main.py"
exec ./run.sh
