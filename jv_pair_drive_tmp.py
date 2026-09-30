"""Focus JARVIS, press ⚙ -> REMOTE CONTROL, verify the QR overlay appeared."""
import os
import sys
import time
from io import BytesIO

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from PIL import Image
from actions.screen_processor import _capture_screen
from core import input_bridge

input_bridge.detect(force=True)
SX, SY = 1366 / 1280, 768 / 720  # screen px -> uinput device px


def shot() -> Image.Image:
    b, _ = _capture_screen()
    return Image.open(BytesIO(b)).convert("RGB")


def dark_ratio(im: Image.Image) -> float:
    px = im.load()
    w, h = im.size
    n = tot = 0
    for y in range(0, h, 6):
        for x in range(0, w, 6):
            tot += 1
            r, g, b = px[x, y]
            if r < 30 and g < 34 and b < 42:
                n += 1
    return n / max(tot, 1)


def center_white(im: Image.Image) -> int:
    """Near-white pixels in the central band — a QR panel lights this up."""
    px = im.load()
    w, h = im.size
    n = 0
    for y in range(int(h * 0.25), int(h * 0.8), 3):
        for x in range(int(w * 0.3), int(w * 0.7), 3):
            r, g, b = px[x, y]
            if r > 225 and g > 225 and b > 225:
                n += 1
    return n


def click(wx: int, wy: int):
    input_bridge._ui_click(int(wx * SX), int(wy * SY))


# 1. focus loop
dr = dark_ratio(shot())
for attempt in range(6):
    if dr > 0.45:
        break
    input_bridge._ui_press("esc")
    time.sleep(0.5)
    input_bridge._ui_hotkey("alt", "tab") if attempt % 2 == 0 else input_bridge._ui_hotkey("super", "tab")
    time.sleep(1.8)
    dr = dark_ratio(shot())
print(f"focus dark: {dr:.2%}")
assert dr > 0.45, "could not bring JARVIS to front"

# 2. open the drawer
before = center_white(shot())
click(25, 40)
time.sleep(1.2)
im = shot()
im.save("/tmp/jv_drawer.png")
print(f"after ⚙: white={center_white(im)} (was {before})")

# 3. press REMOTE CONTROL (first button in the drawer)
click(122, 95)
time.sleep(2.0)
im = shot()
im.save("/tmp/jv_remote.png")
after = center_white(im)
print(f"after REMOTE: white={after}")
print("QR_OVERLAY=" + ("visible" if after > max(before, 800) * 1.5 and after > 1500 else "not-detected"))
