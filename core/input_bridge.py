"""
input_bridge.py — session-aware input injection for JARVIS.

pyautogui drives the mouse and keyboard through the X11 test API (XTEST).
Under Wayland the compositor ignores synthetic X11 input for NATIVE apps, so
clicks / typing silently do nothing there (XWayland apps still work).

On Wayland this module patches pyautogui so input is injected at the KERNEL
level through a virtual uinput device — the same mechanism real mouse/keyboard
drivers use. Every application (native Wayland included) receives those events,
because they arrive below the compositor, exactly like physical hardware.

    Requires only write access to /dev/uinput (udev rule MODE="0666").
    No daemons, no external binaries — pure Python via ioctl/write.

    pyautogui.click(x, y)      → absolute move + button press/release
    pyautogui.hotkey("ctrl","s") → key-down/up sequence (EVDEV codes)
    pyautogui.write(s)         → per-character key events (US layout);
                                 non-ASCII falls back to clipboard paste
    pyautogui.scroll(n)        → REL_WHEEL events
    pyautogui.dragTo(x, y)     → button hold + interpolated moves
    pyautogui.screenshot()     → the Wayland screen-capture path in
                                 actions.screen_processor (X11 shots are black)

On a plain X11 session the patch is a no-op: native pyautogui works there.
"""
from __future__ import annotations

import fcntl
import os
import struct
import threading
import time

# ── Linux input constants (linux/input-event-codes.h, linux/uinput.h) ───────

EV_SYN, EV_KEY, EV_REL, EV_ABS = 0x00, 0x01, 0x02, 0x03
SYN_REPORT = 0
ABS_X, ABS_Y = 0, 1
REL_WHEEL, REL_HWHEEL = 8, 6
BTN_LEFT, BTN_RIGHT, BTN_MIDDLE = 0x110, 0x111, 0x112
INPUT_PROP_POINTER, INPUT_PROP_DIRECT = 0x001, 0x002

# ioctl numbers (constants are ABI-stable since kernel 3.x)
UI_SET_EVBIT   = 0x40045564   # _IOW('U', 100, int)
UI_SET_KEYBIT  = 0x40045565   # _IOW('U', 101, int)
UI_SET_RELBIT  = 0x40045566   # _IOW('U', 102, int)
UI_SET_ABSBIT  = 0x40045567   # _IOW('U', 103, int)
UI_SET_PROPBIT = 0x4004556D   # _IOW('U', 109, int)
UI_DEV_CREATE  = 0x5501       # _IO('U', 1)
UI_DEV_DESTROY = 0x5502       # _IO('U', 2)

_BUTTON = {"left": BTN_LEFT, "right": BTN_RIGHT, "middle": BTN_MIDDLE}

# ── EVDEV key codes (for keyboard) ──────────────────────────────────────────

_EVDEV = {
    "esc": 1, "escape": 1,
    "1": 2, "2": 3, "3": 4, "4": 5, "5": 6, "6": 7, "7": 8, "8": 9, "9": 10, "0": 11,
    "minus": 12, "-": 12, "equal": 13, "=": 13,
    "backspace": 14, "tab": 15,
    "q": 16, "w": 17, "e": 18, "r": 19, "t": 20, "y": 21, "u": 22, "i": 23, "o": 24, "p": 25,
    "bracketleft": 26, "[": 26, "bracketright": 27, "]": 27,
    "enter": 28, "return": 28, "leftctrl": 29,
    "a": 30, "s": 31, "d": 32, "f": 33, "g": 34, "h": 35, "j": 36, "k": 37, "l": 38,
    "semicolon": 39, ";": 39, "apostrophe": 40, "'": 40, "grave": 41, "`": 41,
    "leftshift": 42, "backslash": 43, "\\": 43,
    "z": 44, "x": 45, "c": 46, "v": 47, "b": 48, "n": 49, "m": 50,
    "comma": 51, ",": 51, "dot": 52, ".": 52, "slash": 53, "/": 53,
    "rightshift": 54, "leftalt": 56, "alt": 56, "space": 57, "capslock": 58, "caps": 58,
    "f1": 59, "f2": 60, "f3": 61, "f4": 62, "f5": 63, "f6": 64,
    "f7": 65, "f8": 66, "f9": 67, "f10": 68, "f11": 87, "f12": 88,
    "home": 102, "up": 103, "pageup": 104, "pgup": 104,
    "left": 105, "right": 106, "end": 107, "down": 108, "pagedown": 109, "pgdn": 109,
    "insert": 110, "ins": 110, "delete": 111, "del": 111,
    "print": 99, "printscreen": 99,
    "leftmeta": 125, "super": 125, "super_l": 125, "meta": 125, "win": 125,
    "cmd": 125, "command": 125, "rightmeta": 126, "super_r": 126,
    "ctrl": 29, "control": 29, "ctrl_l": 29, "ctrl_r": 97, "control_r": 97,
    "shift": 42, "shift_l": 42, "shift_r": 54,
    "alt_r": 100, "altgr": 100,
}

# US-layout shifted characters: char → (base key name, needs shift)
_CHARKEY = {
    "a": ("a", False), "b": ("b", False), "c": ("c", False), "d": ("d", False),
    "e": ("e", False), "f": ("f", False), "g": ("g", False), "h": ("h", False),
    "i": ("i", False), "j": ("j", False), "k": ("k", False), "l": ("l", False),
    "m": ("m", False), "n": ("n", False), "o": ("o", False), "p": ("p", False),
    "q": ("q", False), "r": ("r", False), "s": ("s", False), "t": ("t", False),
    "u": ("u", False), "v": ("v", False), "w": ("w", False), "x": ("x", False),
    "y": ("y", False), "z": ("z", False),
    "1": ("1", False), "2": ("2", False), "3": ("3", False), "4": ("4", False),
    "5": ("5", False), "6": ("6", False), "7": ("7", False), "8": ("8", False),
    "9": ("9", False), "0": ("0", False),
    " ": ("space", False), "\n": ("enter", False), "\t": ("tab", False),
    "-": ("minus", False), "=": ("equal", False), "[": ("bracketleft", False),
    "]": ("bracketright", False), "\\": ("backslash", False), ";": ("semicolon", False),
    "'": ("apostrophe", False), "`": ("grave", False), ",": ("comma", False),
    ".": ("dot", False), "/": ("slash", False),
    "!": ("1", True), "@": ("2", True), "#": ("3", True), "$": ("4", True),
    "%": ("5", True), "^": ("6", True), "&": ("7", True), "*": ("8", True),
    "(": ("9", True), ")": ("0", True), "_": ("minus", True), "+": ("equal", True),
    "{": ("bracketleft", True), "}": ("bracketright", True), "|": ("backslash", True),
    ":": ("semicolon", True), '"': ("apostrophe", True), "~": ("grave", True),
    "<": ("comma", True), ">": ("dot", True), "?": ("slash", True),
}

# ── Virtual input device ────────────────────────────────────────────────────

class UInput:
    """Virtual keyboard + absolute/relative pointer via /dev/uinput.

    Uses the LEGACY device setup (write a uinput_user_dev struct then
    UI_DEV_CREATE) because it works on every kernel from 3.x to current,
    with no version-dependent ioctls.
    """

    def __init__(self, width: int, height: int):
        self._lock = threading.Lock()
        self.width, self.height = width, height
        self.x, self.y = width // 2, height // 2   # assumed pointer position
        self.fd = os.open("/dev/uinput", os.O_WRONLY | os.O_NONBLOCK)

        def setbit(ioctl_num: int, bit: int) -> None:
            fcntl.ioctl(self.fd, ioctl_num, bit)

        # capabilities
        for ev in (EV_SYN, EV_KEY, EV_REL, EV_ABS):
            setbit(UI_SET_EVBIT, ev)
        for code in sorted(_EVDEV.values()):
            setbit(UI_SET_KEYBIT, code)
        for btn in (BTN_LEFT, BTN_RIGHT, BTN_MIDDLE):
            setbit(UI_SET_KEYBIT, btn)
        for rel in (REL_WHEEL, REL_HWHEEL):
            setbit(UI_SET_RELBIT, rel)
        for absm in (ABS_X, ABS_Y):
            setbit(UI_SET_ABSBIT, absm)
        setbit(UI_SET_PROPBIT, INPUT_PROP_POINTER)
        setbit(UI_SET_PROPBIT, INPUT_PROP_DIRECT)

        # legacy uinput_user_dev: name[80] + input_id(4×u16) + ff_effects_max
        # + absmax[64] + absmin[64] + absfuzz[64] + absflat[64]
        name = b"Jarvis Virtual Input"
        ident = struct.pack("<4H", 0x06, 0x1, 0x1, 0x01)   # BusUSB, vendor, product, version
        absmax = [0] * 64
        absmin = [0] * 64
        absmax[ABS_X], absmin[ABS_X] = max(0, width - 1), 0
        absmax[ABS_Y], absmin[ABS_Y] = max(0, height - 1), 0
        setup = (struct.pack("<80s", name.ljust(80, b"\0")) + ident
                 + struct.pack("<I", 0)
                 + struct.pack("<64i", *absmax) + struct.pack("<64i", *absmin)
                 + struct.pack("<64i", *([0] * 64)) + struct.pack("<64i", *([0] * 64)))
        os.write(self.fd, setup)
        fcntl.ioctl(self.fd, UI_DEV_CREATE)
        # Give udev a beat to register the device node.
        time.sleep(0.1)

    def _emit(self, etype: int, code: int, value: int) -> None:
        os.write(self.fd, struct.pack("<qqHHi", 0, 0, etype, code, value))

    def _syn(self) -> None:
        self._emit(EV_SYN, SYN_REPORT, 0)

    # -- primitives ---------------------------------------------------------

    def key(self, code: int, down: bool) -> None:
        with self._lock:
            self._emit(EV_KEY, code, 1 if down else 0)
            self._syn()

    def tap(self, code: int, delay: float = 0.03) -> None:
        self.key(code, True)
        time.sleep(delay)
        self.key(code, False)

    def move_abs(self, x: int, y: int) -> None:
        x = max(0, min(int(x), self.width - 1))
        y = max(0, min(int(y), self.height - 1))
        with self._lock:
            self._emit(EV_ABS, ABS_X, x)
            self._emit(EV_ABS, ABS_Y, y)
            self._syn()
        self.x, self.y = x, y

    def move_rel(self, dx: int, dy: int) -> None:
        with self._lock:
            if dx:
                self._emit(EV_REL, 0, int(dx))   # REL_X
            if dy:
                self._emit(EV_REL, 1, int(dy))   # REL_Y
            self._syn()

    def wheel(self, amount: int, horizontal: bool = False) -> None:
        rel = REL_HWHEEL if horizontal else REL_WHEEL
        with self._lock:
            for _ in range(abs(int(amount))):
                self._emit(EV_REL, rel, 1 if amount > 0 else -1)
                self._syn()
                time.sleep(0.01)

    # -- composed actions ---------------------------------------------------

    def click(self, button: str = "left", clicks: int = 1) -> None:
        code = _BUTTON.get(button)
        if code is None:
            raise ValueError(f"unknown button: {button}")
        for _ in range(max(1, int(clicks))):
            self.key(code, True)
            time.sleep(0.04)
            self.key(code, False)
            time.sleep(0.06)

    def hotkey(self, *names) -> None:
        codes = []
        for n in names:
            k = str(n).strip().lower()
            if k not in _EVDEV:
                raise ValueError(f"key '{n}' not mapped in core/input_bridge.py")
            codes.append(_EVDEV[k])
        if not codes:
            return
        if len(codes) == 1:
            self.tap(codes[0])
            return
        for c in codes[:-1]:
            self.key(c, True)
        time.sleep(0.02)
        self.tap(codes[-1])
        for c in reversed(codes[:-1]):
            self.key(c, False)

    def type_char(self, ch: str) -> None:
        entry = _CHARKEY.get(ch) or _CHARKEY.get(ch.lower())
        if entry is None:
            raise ValueError(f"character {ch!r} not typeable on US layout")
        name, shift = entry
        if ch.isupper():                 # 'U' → shift+u (table stores lowercase)
            shift = True
        code = _EVDEV[name]
        if shift:
            self.key(_EVDEV["leftshift"], True)
        self.tap(code, delay=0.02)
        if shift:
            self.key(_EVDEV["leftshift"], False)

    def close(self) -> None:
        try:
            fcntl.ioctl(self.fd, UI_DEV_DESTROY)
        except Exception:
            pass
        try:
            os.close(self.fd)
        except Exception:
            pass


# ── Device management ───────────────────────────────────────────────────────

_dev: UInput | None = None
_dev_lock = threading.Lock()


def _screen_size() -> tuple[int, int]:
    try:
        import pyautogui
        s = pyautogui.size()
        return int(s.width), int(s.height)
    except Exception:
        pass
    try:
        from actions.screen_processor import _capture_screen
        from PIL import Image
        import io
        b, _ = _capture_screen()
        return Image.open(io.BytesIO(b)).size
    except Exception:
        return 1366, 768


def _get_dev() -> UInput:
    global _dev
    with _dev_lock:
        if _dev is None:
            w, h = _screen_size()
            _dev = UInput(w, h)
        return _dev


# ── pyautogui-compatible implementations ────────────────────────────────────

def _ui_move_to(x=None, y=None, duration=0.0, **k) -> None:
    if x is None or y is None:
        raise ValueError("moveTo needs x and y")
    dev = _get_dev()
    if duration:
        cx, cy = dev.x, dev.y
        steps = max(2, int(duration / 0.01))
        for i in range(1, steps + 1):
            dev.move_abs(cx + (x - cx) * i / steps, cy + (y - cy) * i / steps)
            time.sleep(duration / steps)
    else:
        dev.move_abs(x, y)


def _ui_click(x=None, y=None, clicks=1, interval=0.0, button="left", **k) -> None:
    if x is not None and y is not None:
        _ui_move_to(x, y)
        time.sleep(0.05)
    dev = _get_dev()
    for _ in range(max(1, int(clicks))):
        dev.click(button, 1)
        if interval:
            time.sleep(interval)


def _ui_press(key, **k) -> None:
    name = str(key).strip().lower()
    if name not in _EVDEV:
        raise ValueError(f"key '{key}' not mapped in core/input_bridge.py")
    _get_dev().tap(_EVDEV[name])


def _ui_hotkey(*keys, **k) -> None:
    _get_dev().hotkey(*keys)


def _ui_key_down(key) -> None:
    name = str(key).strip().lower()
    if name not in _EVDEV:
        raise ValueError(f"key '{key}' not mapped in core/input_bridge.py")
    _get_dev().key(_EVDEV[name], True)


def _ui_key_up(key) -> None:
    name = str(key).strip().lower()
    if name not in _EVDEV:
        raise ValueError(f"key '{key}' not mapped in core/input_bridge.py")
    _get_dev().key(_EVDEV[name], False)


def _ui_mouse_down(x=None, y=None, button="left", **k) -> None:
    if x is not None and y is not None:
        _ui_move_to(x, y)
        time.sleep(0.05)
    _get_dev().key(_BUTTON.get(button, BTN_LEFT), True)


def _ui_mouse_up(x=None, y=None, button="left", **k) -> None:
    if x is not None and y is not None:
        _ui_move_to(x, y)
        time.sleep(0.05)
    _get_dev().key(_BUTTON.get(button, BTN_LEFT), False)


def _ui_drag_to(x=None, y=None, duration=0.3, button="left", **k) -> None:
    if x is None or y is None:
        raise ValueError("dragTo needs x and y")
    dev = _get_dev()
    cx, cy = dev.x, dev.y
    dev.key(_BUTTON.get(button, BTN_LEFT), True)
    time.sleep(0.08)
    steps = max(4, int((duration or 0.3) / 0.015))
    for i in range(1, steps + 1):
        dev.move_abs(cx + (x - cx) * i / steps, cy + (y - cy) * i / steps)
        time.sleep(duration / steps if duration else 0.01)
    time.sleep(0.08)
    dev.key(_BUTTON.get(button, BTN_LEFT), False)


def _ui_write(text: str, interval: float = 0.0, **k) -> None:
    """Type text character-by-character on a US layout. Non-ASCII characters
    (the text can be any language) fall back to clipboard paste; if that is
    impossible, they are skipped with a warning instead of failing the action."""
    dev = _get_dev()
    pending_non_ascii = []
    for ch in str(text):
        try:
            dev.type_char(ch)
        except ValueError:
            pending_non_ascii.append(ch)
            continue
        if interval:
            time.sleep(interval)
    if not pending_non_ascii:
        return
    chunk = "".join(pending_non_ascii)
    try:
        import pyperclip
        pyperclip.copy(chunk)
        dev.hotkey("ctrl", "v")
    except Exception:
        print(f"[Input] ⚠️  could not type non-ASCII text: {chunk[:40]!r}")


def _ui_scroll(amount: int, **k) -> None:
    _get_dev().wheel(int(amount), horizontal=False)


def _ui_hscroll(amount: int, **k) -> None:
    _get_dev().wheel(int(amount), horizontal=True)


def _bridge_screenshot(*args, **kwargs):
    """Full-screen capture through the Wayland-capable path (X11 screenshots
    from pyautogui come back black under Wayland)."""
    if args or kwargs.get("region"):
        raise NotImplementedError("screenshot(region=…) is not supported on Wayland — "
                                  "crop the full image instead")
    from io import BytesIO
    from PIL import Image
    from actions.screen_processor import _capture_screen
    img_bytes, _mime = _capture_screen()
    return Image.open(BytesIO(img_bytes))


# ── Patching ────────────────────────────────────────────────────────────────

def _ui_position():
    """Wayland: pyautogui.position() reads the X11 pointer view, which does NOT
    track the real (uinput-injected) pointer — report our own tracked position
    instead."""
    dev = _get_dev()
    import pyautogui as _p
    return _p.Point(dev.x, dev.y)


_PATCHED = {
    "position":   _ui_position,
    "click":      _ui_click,
    "moveTo":     _ui_move_to,
    "press":      _ui_press,
    "hotkey":     _ui_hotkey,
    "keyDown":    _ui_key_down,
    "keyUp":      _ui_key_up,
    "mouseDown":  _ui_mouse_down,
    "mouseUp":    _ui_mouse_up,
    "dragTo":     _ui_drag_to,
    "write":      _ui_write,
    "typewrite":  _ui_write,
    "scroll":     _ui_scroll,
    "hscroll":    _ui_hscroll,
    "screenshot": _bridge_screenshot,
}


def _is_wayland() -> bool:
    return bool((os.environ.get("WAYLAND_DISPLAY") or "").strip())


_lock  = threading.Lock()
_state = None


def detect(force: bool = False) -> dict:
    """Decide which input backend to use. Result is cached for the process."""
    global _state
    with _lock:
        if _state is not None and not force:
            return _state
        if not _is_wayland():
            _state = {"mode": "x11", "why": "X11 session — native pyautogui"}
            return _state
        if not os.access("/dev/uinput", os.R_OK | os.W_OK):
            _state = {"mode": "none",
                      "why": "Wayland session but /dev/uinput is not writable — "
                             "clicks/typing cannot work. Fix:  "
                             "echo 'KERNEL==\"uinput\", MODE=\"0666\"' | sudo tee /etc/udev/rules.d/80-uinput.rules"}
            return _state
        try:
            w, h = _screen_size()
            UInput(w, h).close()   # probe: can we actually create a device?
        except Exception as e:
            _state = {"mode": "none",
                      "why": f"Wayland session but uinput device creation failed: {e}"}
            return _state
        _state = {"mode": "uinput", "why": f"Wayland — injecting via /dev/uinput ({w}×{h} pointer)"}
        return _state


def apply_patch(verbose: bool = True) -> dict:
    """Call once at startup. On Wayland, replaces the pyautogui functions that
    inject input with kernel-level uinput equivalents. Returns the state dict."""
    st = detect()
    if st["mode"] == "uinput":
        import pyautogui
        for name, fn in _PATCHED.items():
            setattr(pyautogui, name, fn)
        if verbose:
            print(f"[Input] {st['why']} — clicks/typing/hotkeys/scroll/drag patched")
    elif verbose:
        print(f"[Input] {st['why']}")
    return st
