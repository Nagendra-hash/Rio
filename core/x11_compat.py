"""x11_compat.py — make python-xlib accept the X cookie Mutter hands out.

THE PROBLEM
    On a GNOME/Wayland session JARVIS talks to XWayland through the auth file
    Mutter writes for it:

        DISPLAY=:0
        XAUTHORITY=/run/user/1000/.mutter-Xwaylandauth.XXXXXX

    That file holds the cookie in two entries whose *display number field is
    empty*:

        family 256   (FamilyLocal)   addr nagendra-Inspiron-3542   num b''
        family 65535 (FamilyWild)    addr nagendra-Inspiron-3542   num b''

    libXau — what Qt, xdpyinfo and every C client use — treats a missing number
    as a wildcard, so those clients connect normally. python-xlib does not: its
    `Xauthority.get_best_auth()` compares the entry number against the literal
    b"0" and therefore finds no match, sends no cookie, and the server answers

        Authorization required, but no authorization protocol specified

    which python-xlib raises as `DisplayConnectionError`. That is the error the
    desktop actions were dying on at IMPORT time — so `computer_control`,
    `computer_settings`, `desktop` and `send_message` were dropped from the tool
    registry entirely, and JARVIS reported that it could not control the
    computer at all.

WHAT THIS DOES
    Wraps `Xauthority.get_best_auth` so that, ONLY after the unpatched method
    has failed to find a cookie, it retries with the empty display number and
    empty address accepted as wildcards — exactly libXau's documented behaviour.
    The normal path is unchanged and tried first, so nothing about a machine
    with a conventional ~/.Xauthority changes.

WHAT THIS DOES NOT DO
    It never reads, copies, prints or moves the cookie. It only relaxes which
    *entry* is chosen from the file the environment already points at, and it
    leaves the file's own permissions alone. It is not `xhost +`: no access is
    granted to anybody who could not already connect with xdpyinfo.

The patch is idempotent and safe to call from several modules; `install()` is
invoked once at the top of main.py and again by `core.desktop_input` before it
imports pyautogui, because an action may be imported by a test or a plugin
that never goes through main.py.
"""

from __future__ import annotations

import os

_FAMILY_WILD = 65535          # Xauth FamilyWild
_PATCHED_FLAG = "_jarvis_xwayland_wildcard"


def _entries_of(authority) -> list[tuple]:
    """The (family, address, number, name, data) tuples, or [] if unreadable."""
    try:
        return list(authority.entries)
    except Exception:
        return []


def _relaxed_lookup(authority, family, address, dispno,
                    types=(b"MIT-MAGIC-COOKIE-1",)):
    """libXau's matching rule: an empty number or address is a wildcard."""
    num = str(dispno).encode()
    addr = address.encode() if isinstance(address, str) else address

    matches: dict[bytes, bytes] = {}
    for efam, eaddr, enum, ename, edata in _entries_of(authority):
        if efam not in (family, _FAMILY_WILD):
            continue
        if eaddr not in (addr, b""):
            continue
        if enum not in (num, b""):
            continue
        matches.setdefault(ename, edata)

    for want in types:
        if want in matches:
            return (want, matches[want])
    return None


def install() -> bool:
    """Patch python-xlib in this process. True if the shim is in place.

    Returns False when python-xlib is not installed at all — that is not an
    error here, it just means there is nothing to patch (pyautogui absent, or a
    platform that does not use X11).
    """
    try:
        from Xlib import xauth as _xauth
    except Exception:
        return False

    original = _xauth.Xauthority.get_best_auth
    if getattr(original, _PATCHED_FLAG, False):
        return True

    def get_best_auth(self, family, address, dispno,
                      types=(b"MIT-MAGIC-COOKIE-1",)):
        # The unpatched rule first: a conventional Xauthority entry still wins,
        # with no change in behaviour.
        try:
            return original(self, family, address, dispno, types)
        except Exception as exc:              # XNoAuthError, or a malformed file
            found = _relaxed_lookup(self, family, address, dispno, types)
            if found is not None:
                return found
            raise exc

    setattr(get_best_auth, _PATCHED_FLAG, True)
    _xauth.Xauthority.get_best_auth = get_best_auth
    return True


def describe() -> str:
    """One line for the startup log — where the display and its cookie live."""

    display = os.environ.get("DISPLAY", "") or "(unset)"
    auth = os.environ.get("XAUTHORITY", "") or "(default ~/.Xauthority)"
    cached = install()
    return (f"X11: DISPLAY={display} XAUTHORITY={auth} "
            f"libxau-compat={'on' if cached else 'n/a'}")
