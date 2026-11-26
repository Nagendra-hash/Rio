"""
android_control.py — JARVIS controls an Android phone over ADB (USB or Wi-Fi).

One tool, many sub-actions. The first call with no device connected walks the
user through setup (setup_guide / pair). Self-healing: if the `adb` binary is
missing, core/self_heal raises a GUI polkit prompt to install it — the user
approves with their password and the tool retries automatically.

Setup, in plain steps (also spoken by `setup_guide`):
  PHONE (once):
    1. Settings → About phone → tap "Build number" 7× (Developer mode)
    2. Settings → System → Developer options → enable "USB debugging"
       (enable "Wireless debugging" too, for Wi-Fi pairing)
  PC: nothing — adb is auto-installed on first use via a password prompt.

Pairing:
  USB (easiest): plug the phone in, tap "Allow" on the "Allow USB debugging?"
  prompt on the phone screen. Done.
  Wi-Fi (Android 11+): Settings → Developer options → Wireless debugging →
  "Pair device with pairing code" → tell JARVIS the 6-digit code and the
  IP:port it shows (action `pair`, params code + address).

Every action returns a human-readable string; nothing raises. All shell
interaction goes through adb with a timeout and no shell injection surface
(arguments are passed as a list, never through a shell string).
"""

from __future__ import annotations

import re
import shutil
import subprocess
import time

# ── adb access ───────────────────────────────────────────────────────────────


def _adb_path() -> str | None:
    """Prefer the newest adb available: ~/.local/bin (user-installed Google
    platform-tools) beats Ubuntu's often-ancient /usr/bin/adb (which lacks
    `adb pair` for Android-11 wireless pairing). Falls back to PATH, then to
    known locations directly (systemd services may have a thin PATH)."""
    found = shutil.which("adb")
    if found:
        return found
    from pathlib import Path as _P

    for cand in (_P.home() / ".local" / "bin" / "adb", _P("/usr/bin/adb")):
        if cand.is_file():
            return str(cand)
    return None


def _adb(*args: str, timeout: int = 20, device: str | None = None) -> tuple[bool, str]:
    """Run one adb command. Returns (ok, output). Never raises.
    If adb itself is missing, self-heal installs it (GUI password prompt) and
    we retry the lookup once."""
    exe = _adb_path()
    if exe is None:
        try:
            from core.self_heal import ensure_binary

            diag, ok = ensure_binary("adb")
            print(f"[Android] self-heal: {diag}")
        except Exception as e:
            print(f"[Android] self-heal unavailable: {e}")
        exe = _adb_path()
        if exe is None:
            return False, "adb is not installed and auto-install did not succeed."
    cmd = [exe] + (["-s", device] if device else []) + list(args)
    try:
        r = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            encoding="utf-8",
            errors="replace",
        )
        out = (r.stdout or "").strip()
        err = (r.stderr or "").strip()
        return r.returncode == 0, (out or err)
    except subprocess.TimeoutExpired:
        return False, f"adb {' '.join(args[:2])} timed out after {timeout}s"
    except Exception as e:
        return False, f"adb error: {e}"


def _devices() -> list[str]:
    ok, out = _adb("devices", timeout=10)
    if not ok:
        return []
    devs = []
    for line in out.splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 2 and parts[1] == "device":
            devs.append(parts[0])
    return devs


def _one_device() -> tuple[str | None, str]:
    """The single connected device — or, if pairing exists but the connection
    dropped (wireless debugging restarts change the port), auto-reconnect via
    mDNS discovery before giving up."""
    devs = _devices()
    if devs:
        return devs[0], ""
    # Pairing persists across port changes: discover the new connection port.
    ok, out = _adb("mdns", "services", timeout=10)
    if ok:
        for line in out.splitlines():
            if "_adb-tls-connect._tcp" in line:
                parts = line.split("\t")
                addr = parts[-1].strip()
                if ":" in addr:
                    _adb("connect", addr, timeout=15)
                    time.sleep(1)
                    devs = _devices()
                    if devs:
                        print(f"[Android] auto-reconnected via mDNS: {devs[0]}")
                        return devs[0], ""
    return None, (
        "No phone connected. Run the `pair` setup (say 'setup my phone') "
        "or plug the phone in via USB and tap 'Allow' on the debugging prompt."
    )


# ── helpers ──────────────────────────────────────────────────────────────────


def _sh(device: str, command: str, timeout: int = 25) -> str:
    ok, out = _adb("-s", device, "shell", command, timeout=timeout)
    return out if ok is not None else out  # ok flag unused; out carries result


def _open_url(device: str, url: str) -> str:
    ok, out = _adb(
        "-s",
        device,
        "shell",
        "am",
        "start",
        "-a",
        "android.intent.action.VIEW",
        "-d",
        url,
        timeout=20,
    )
    return "Opened link on phone." if ok else f"Could not open link: {out}"


def _screenshot(device: str) -> str:
    remote = "/sdcard/jarvis_screen.png"
    local = "/tmp/jarvis_phone_screen.png"
    ok1, o1 = _adb("-s", device, "shell", "screencap", "-p", remote, timeout=20)
    if not ok1:
        return f"Screenshot failed: {o1}"
    ok2, o2 = _adb("-s", device, "pull", remote, local, timeout=20)
    if not ok2:
        return f"Could not copy screenshot: {o2}"
    _adb("-s", device, "shell", "rm", remote, timeout=10)
    return f"Screenshot saved: {local}"


# ── setup flows ──────────────────────────────────────────────────────────────

_SETUP_STEPS = (
    "One-time phone setup, sir: "
    "One — open Settings, About phone, and tap Build number seven times to "
    "unlock Developer options. "
    "Two — in Settings, System, Developer options, enable USB debugging, and "
    "also Wireless debugging if you want a cable-free link. "
    "Three — for USB pairing, just plug the phone into this computer and tap "
    "Allow on the debugging prompt on the phone screen. "
    "For Wi-Fi pairing, open Wireless debugging, tap 'Pair device with pairing "
    "code', then tell me the six-digit code and the IP address and port it shows."
)


def _setup_guide() -> str:
    if not _adb_path():
        try:
            from core.self_heal import ensure_binary

            diag, ok = ensure_binary("adb")
            print(f"[Android] self-heal: {diag}")
        except Exception:
            pass
    devs = _devices()
    if devs:
        return f"adb is ready and your phone is connected ({devs[0]}). We're good to go, sir."
    return _SETUP_STEPS + " adb will install itself with a password prompt when needed."


def _pair(code: str, address: str) -> str:
    """Wi-Fi pairing: adb pair ip:port CODE. Then connect to the shown port."""
    code = (code or "").strip()
    address = (address or "").strip()
    if not re.fullmatch(r"\d{6}", code):
        return "I need the six-digit pairing code shown on the phone, sir."
    if ":" not in address:
        return (
            "I need the address as IP:PORT — exactly as the pairing screen shows, sir."
        )
    ok, out = _adb("pair", address, code, timeout=30)
    if not ok:
        return f"Pairing failed: {out}"
    ip = address.split(":")[0]
    # Pairing port ≠ connection port. Modern adb discovers the connection
    # service via mDNS once paired; try that first, then the classic 5555.
    _adb("mdns", "services", timeout=10)
    time.sleep(2)
    ok2, out2 = _adb("connect", f"{ip}:5555", timeout=15)
    devs = _devices()
    if devs:
        return f"Paired and connected — phone reachable at {devs[0]}."
    return (
        f"Pairing succeeded ({out}), but auto-connect did not ({out2}). "
        "Close the pairing dialog and read me the IP:PORT shown on the "
        "MAIN Wireless debugging screen (the connection port), then say connect."
    )


def _connect(address: str) -> str:
    address = (address or "").strip()
    if ":" not in address:
        return "Give me the address as IP:PORT from the Wireless debugging screen, sir."
    ok, out = _adb("connect", address, timeout=15)
    return f"Connected: {out}" if ok else f"Connect failed: {out}"


# ── the tool ─────────────────────────────────────────────────────────────────


def android_control(parameters: dict, player=None, speak=None) -> str:
    params = parameters or {}
    action = (params.get("action") or "").strip().lower()
    device = (params.get("device") or "").strip() or None
    arg = (params.get("arg") or "").strip()
    arg2 = (params.get("arg2") or "").strip()

    def log(s: str):
        if player:
            player.write_log(f"[android] {s[:60]}")

    if not action:
        return "Which phone action? e.g. setup, pair, screenshot, open_app, tap, type, battery."

    # Setup/pairing work without a connected device.
    if action in ("setup", "setup_guide", "guide"):
        result = _setup_guide()
        log(result)
        return result
    if action == "pair":
        result = _pair(arg, arg2)  # arg = 6-digit code, arg2 = ip:port
        log(result)
        return result
    if action == "connect":
        result = _connect(arg or arg2)
        log(result)
        return result

    dev, msg = (device, "") if device else _one_device()
    if dev is None:
        if action in ("devices", "list"):
            result = "No phone connected. " + _SETUP_STEPS
            log(result)
            return result
        log(msg)
        return msg

    if action == "devices" or action == "list":
        result = "Connected phones: " + ", ".join(_devices() or ["none"])
    elif action == "status":
        battery = _adb("-s", dev, "shell", "dumpsys", "battery", timeout=15)[1]
        level = next((l for l in battery.splitlines() if "level" in l), "")
        result = f"Phone {dev} online. Battery: {level.strip() or 'unknown'}."
    elif action == "battery":
        ok, out = _adb("-s", dev, "shell", "dumpsys", "battery", timeout=15)
        m = re.search(r"level:\s*(\d+)", out)
        result = (
            f"Battery is at {m.group(1)} percent."
            if ok and m
            else f"Could not read battery: {out}"
        )
    elif action == "screenshot":
        result = _screenshot(dev)
    elif action == "open_app":
        if not arg:
            result = "Which app should I open?"
        else:
            # Resolve the package FIRST — launching via monkey without a
            # resolved package makes Android open a RANDOM app while still
            # printing 'Events injected: 1' (observed: asked for YouTube,
            # got Facebook). Resolve, then launch the exact package.
            ok, pkgs = _adb("-s", dev, "shell", "pm list packages", timeout=20)
            pkg = ""
            if ok:
                wanted = arg.lower().replace(" ", "")
                names = [
                    l.strip().removeprefix("package:")
                    for l in pkgs.splitlines()
                    if l.strip()
                ]

                # Score candidates so the obvious app wins: exact tail match
                # (com.google.android.youtube for 'YouTube') beats partial
                # matches (com.google.android.apps.youtube.music), and any
                # tail match beats a substring buried in the package id.
                def _score(name: str) -> int:
                    tail = name.lower().split(".")[-1]
                    flat = name.lower().replace(".", "")
                    if tail == wanted:
                        return 3
                    if wanted in tail:
                        return 2
                    if wanted in flat:
                        return 1
                    return 0

                scored = sorted(((_score(n), -len(n), n) for n in names), reverse=True)
                if scored and scored[0][0] > 0:
                    pkg = scored[0][2]
            if not pkg:
                result = (
                    f"I couldn't find an app named '{arg}' on the phone, "
                    "sir. It may not be installed."
                )
            else:
                ok2, out2 = _adb(
                    "-s",
                    dev,
                    "shell",
                    "monkey",
                    "-p",
                    pkg,
                    "-c",
                    "android.intent.category.LAUNCHER",
                    "1",
                    timeout=30,
                )
                result = (
                    f"Opened '{arg}' ({pkg}) on the phone."
                    if ok2 and "Events injected: 1" in out2
                    else f"Could not launch '{arg}' ({pkg}): {out2[:120]}"
                )
    elif action == "youtube":
        # 'play X on youtube' — open search results in the YouTube app (the
        # app intercepts youtube.com links). Tapping the video stays with the
        # user: Android blocks silent playback automation by design.
        import urllib.parse as _up

        q = arg or params.get("text") or ""
        if not q:
            result = "What should I play on YouTube?"
        else:
            result = _open_url(
                dev, "https://www.youtube.com/results?search_query=" + _up.quote(q)
            )
            result = result.replace(
                "Opened link on phone.",
                f"YouTube is showing results for '{q}' — tap the one you like, sir.",
            )
    elif action == "open_url":
        result = _open_url(dev, arg) if arg else "Which link?"
    elif action == "tap":
        m = re.fullmatch(r"(\d+)[,\s]+(\d+)", arg)
        if not m:
            result = "Give me tap coordinates as x,y — or use screen_text instead."
        else:
            ok, out = _adb(
                "-s", dev, "shell", "input", "tap", m.group(1), m.group(2), timeout=15
            )
            result = (
                f"Tapped {m.group(1)},{m.group(2)}." if ok else f"Tap failed: {out}"
            )
    elif action == "swipe":
        m = re.fullmatch(r"(\d+)[,\s]+(\d+)[,\s]+(\d+)[,\s]+(\d+)(?:[,\s]+(\d+))?", arg)
        if not m:
            result = "Swipe needs x1,y1,x2,y2 and optional duration ms."
        else:
            args = [m.group(1), m.group(2), m.group(3), m.group(4)] + (
                [m.group(5)] if m.group(5) else []
            )
            ok, out = _adb("-s", dev, "shell", "input", "swipe", *args, timeout=20)
            result = "Swiped." if ok else f"Swipe failed: {out}"
    elif action == "type":
        text = params.get("text") or arg
        if not text:
            result = "What should I type?"
        else:
            safe = text.replace(" ", "%s").replace("'", "")
            ok, out = _adb("-s", dev, "shell", "input", "text", safe, timeout=20)
            result = "Typed on the phone." if ok else f"Type failed: {out}"
    elif action == "key":
        keymap = {
            "home": "KEYCODE_HOME",
            "back": "KEYCODE_BACK",
            "enter": "KEYCODE_ENTER",
            "power": "KEYCODE_POWER",
            "volume_up": "KEYCODE_VOLUME_UP",
            "volume_down": "KEYCODE_VOLUME_DOWN",
            "recents": "KEYCODE_APP_SWITCH",
        }
        kc = keymap.get(arg.lower())
        result = (
            (
                _adb("-s", dev, "shell", "input", "keyevent", kc, timeout=15)[1]
                or f"Pressed {arg}."
            )
            if kc
            else f"Unknown key '{arg}'. Try: {', '.join(keymap)}"
        )
    elif action == "screen_text":
        ok, out = _adb("-s", dev, "exec-out", "uiautomator dump /dev/tty", timeout=30)
        result = (
            (out[-3000:] if out else "Could not read the phone screen.")
            if ok
            else f"Screen read failed: {out}"
        )
    elif action == "notification":
        # Show heads-up notification via JARVIS' own app-less trick: open the
        # notification shade instead.
        ok, out = _adb(
            "-s", dev, "shell", "cmd", "statusbar", "expand-notifications", timeout=15
        )
        result = "Notification shade opened on the phone." if ok else f"Failed: {out}"
    elif action == "call":
        if not re.fullmatch(r"[+\d][\d\s\-]{5,}", arg):
            result = "Give me a phone number to call, sir."
        else:
            result = _open_url(dev, "tel:" + re.sub(r"[^\d+]", "", arg))
    elif action == "sms":
        number, body = arg, (params.get("text") or arg2)
        if not number or not body:
            result = "I need a number and a message text, sir."
        else:
            _adb(
                "-s",
                dev,
                "shell",
                "am",
                "start",
                "-a",
                "android.intent.action.SENDTO",
                "-d",
                "smsto:" + re.sub(r"[^\d+]", "", number),
                timeout=20,
            )
            ok, out = _adb(
                "-s",
                dev,
                "shell",
                "input",
                "text",
                body.replace(" ", "%s").replace("'", ""),
                timeout=15,
            )
            result = "SMS draft ready on the phone screen — press send there (Android blocks silent sending)."
    elif action == "install_apk":
        if not arg or not arg.endswith(".apk"):
            result = "Give me the path to an .apk file, sir."
        else:
            ok, out = _adb("-s", dev, "install", "-r", arg, timeout=180)
            result = (
                "APK installed on the phone."
                if ok and "Success" in out
                else f"Install failed: {out[:150]}"
            )
    elif action == "uninstall":
        ok, out = (
            _adb("-s", dev, "uninstall", arg, timeout=60)
            if arg
            else (False, "which package?")
        )
        result = f"Removed {arg}." if ok else f"Uninstall failed: {out}"
    elif action == "shell":
        # Escape hatch for everything else — read-only-ish by convention.
        if not arg:
            result = "Which shell command should the phone run?"
        else:
            ok, out = _adb("-s", dev, "shell", arg, timeout=30)
            result = out[:2000] if ok else f"Shell failed: {out[:200]}"
    else:
        result = (
            f"Unknown phone action '{action}'. Available: setup, pair, connect, devices, "
            "status, battery, screenshot, screen_text, open_app, open_url, tap, swipe, "
            "type, key, notification, call, sms, install_apk, uninstall, shell."
        )

    log(result)
    return result


# ── Tool declaration (auto-discovered by core/action_loader.py) ──────────────
TOOL = {
    "name": "android_control",
    "description": (
        "Controls the user's Android phone over ADB — USB or Wi-Fi. Use for: phone "
        "status and battery, screenshots, reading the phone screen, opening apps or "
        "links, tapping/swiping/typing, pressing home/back/power/volume, opening the "
        "notification shade, starting calls or SMS drafts, installing or removing "
        "APKs, and running shell commands on the phone. First-time use or 'pair my "
        "phone' walks the user through enabling USB/Wireless debugging and pairing; "
        "adb is installed automatically with a password prompt when missing."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": (
                    "setup | pair | connect | devices | status | battery | "
                    "screenshot | screen_text | open_app | open_url | tap | "
                    "swipe | type | key | notification | call | sms | "
                    "install_apk | uninstall | shell"
                ),
            },
            "arg": {
                "type": "STRING",
                "description": (
                    "Primary argument: pairing code (pair), IP:PORT "
                    "(connect), app name (open_app), URL (open_url), "
                    "'x,y' (tap), 'x1,y1,x2,y2[,ms]' (swipe), key name "
                    "(key: home/back/enter/power/volume_up/volume_down/"
                    "recents), phone number (call/sms), apk path "
                    "(install_apk), package name (uninstall), command (shell)"
                ),
            },
            "arg2": {
                "type": "STRING",
                "description": "Secondary argument: IP:PORT for pair, message text for sms",
            },
            "text": {
                "type": "STRING",
                "description": "Text to type (type) or SMS body (sms)",
            },
            "device": {
                "type": "STRING",
                "description": "Specific device serial when several phones are connected",
            },
        },
        "required": ["action"],
    },
    "handler": android_control,
}
