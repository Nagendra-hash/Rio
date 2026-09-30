"""Cycle windows until JARVIS (large dark UI) is frontmost. Real input only."""
import os
import sys
import time
from io import BytesIO

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from PIL import Image
from actions.screen_processor import _capture_screen
from core import input_bridge

input_bridge.detect(force=True)


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


dr = dark_ratio(shot())
print(f"start dark: {dr:.2%}")
for attempt in range(6):
    if dr > 0.45:
        print(f"JARVIS_FRONTMOST=yes (attempt {attempt})")
        sys.exit(0)
    # close any stray overview first
    input_bridge._ui_press("esc")
    time.sleep(0.6)
    if attempt % 2 == 0:
        input_bridge._ui_hotkey("alt", "tab")
        print(f"tap {attempt}: alt-tab")
    else:
        input_bridge._ui_hotkey("super", "tab")
        print(f"tap {attempt}: super-tab")
    time.sleep(1.8)
    im = shot()
    im.save(f"/tmp/jv_cycle{attempt}.png")
    dr = dark_ratio(im)
    print(f"  dark now: {dr:.2%}")

print("JARVIS_FRONTMOST=no")
sys.exit(1)
