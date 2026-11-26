#!/usr/bin/env bash
# fix_webcam_autosuspend.sh — stop the internal webcam from dropping off USB.
#
# Symptom this fixes (Inspiron 3542, Integrated_Webcam_HD 0c45:670b):
#   JARVIS opens the camera → the kernel suspends it between uses → the next
#   open wakes it through a reset storm:
#     "Failed to set UVC probe control : -71"
#     "usb 1-1.5: USB disconnect, device number 7"
#     "usb 1-1.5: new high-speed USB device number 8"
#   …which JARVIS reports as "the webcam keeps connecting and disconnecting".
#
# The fix is to pin power/control=on for this one device with a udev rule, so
# the kernel never runtime-suspends it. Needs root once; afterwards it is
# persistent across reboots and re-plugs.
#
# Usage:
#   sudo scripts/fix_webcam_autosuspend.sh
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
    echo "This needs root. Re-running with sudo…" >&2
    exec sudo -- "$0" "$@"
fi

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/webcam-no-autosuspend.rules"
DST="/etc/udev/rules.d/50-jarvis-webcam.rules"

if [ ! -f "$SRC" ]; then
    echo "Missing rule file: $SRC" >&2
    exit 1
fi

install -m 0644 "$SRC" "$DST"
echo "Installed $DST"
udevadm control --reload-rules
echo "udev rules reloaded"

# Deliberately NO `udevadm trigger` here. Re-enumerating this camera makes it
# fail with "device descriptor read/64, error -32" and wedge off the USB bus
# until a full power-drain — verified 2026-11-17. Reloading the rules is
# enough: the rule fires on the next natural plug/re-probe or the next boot,
# and the loop below applies the setting to a camera that is already present.

# Apply now, without waiting for a re-plug: the rule only matches on add.
changed=0
for ctrl in /sys/bus/usb/devices/*/power/control; do
    [ -f "$ctrl" ] || continue
    dev="$(dirname "$(dirname "$ctrl")")"
    vid="$(cat "$dev/idVendor" 2>/dev/null || true)"
    pid="$(cat "$dev/idProduct" 2>/dev/null || true)"
    if [ "$vid" = "0c45" ] && [ "$pid" = "670b" ]; then
        echo on > "$ctrl"
        echo "Set power/control=on for $(basename "$dev") ($vid:$pid)"
        changed=1
    fi
done

[ "$changed" = 1 ] || echo "Webcam not present right now — the rule applies when it next appears."

echo
echo "Verify with:  cat /sys/bus/usb/devices/*/power/control   # camera line must say 'on'"
