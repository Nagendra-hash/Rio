"""The Qt video environment gui/__init__.py sets before the multimedia plugin loads.

The policy is now the opposite of the first AV1 "fix": hardware decoding is left
at Qt's default (auto), because disabling it globally also disabled H.264/VP9
hardware decoding on machines where it works. The AV1 failure is not a hardware
flag at all — the bundled FFmpeg has no software AV1 decoder — and is handled by
core/media_prep.py before the file reaches Qt (see tests/test_media_prep.py).

What must hold here:
  * gui/__init__.py does NOT force QT_FFMPEG_DECODING_HW_DEVICE_TYPES to "",
  * an explicit choice from the user still survives,
  * the module never imports Qt itself (it must run before the plugin loads).

These run the check in a child process: Qt reads the variable once, at plugin
load, and the parent's environment is not a clean slate.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VAR = "QT_FFMPEG_DECODING_HW_DEVICE_TYPES"


def _read(extra_env: dict[str, str]) -> str:
    """Import gui in a child process and report the variable as it ends up."""
    env = {k: v for k, v in os.environ.items() if k != VAR}
    env.update(extra_env)
    out = subprocess.run(
        [sys.executable, "-c",
         f"import gui, os; print(repr(os.environ.get({VAR!r})))"],
        cwd=ROOT, env=env, capture_output=True, text=True, timeout=60,
    )
    assert out.returncode == 0, out.stderr[-2000:]
    return out.stdout.strip()


def test_hardware_decoding_is_not_disabled_globally():
    """Unset means "probe everything", which is Qt's supported auto behaviour."""
    assert _read({}) == "None"


def test_a_machine_that_can_decode_keeps_its_choice():
    assert _read({VAR: "vaapi"}) == "'vaapi'"


def test_the_qt_environment_is_set_before_any_multimedia_import():
    """gui/__init__.py is the one place every route into the HUD passes through,
    and it must not drag Qt in itself."""
    source = (ROOT / "gui" / "__init__.py").read_text(encoding="utf-8")
    assert "QT_MEDIA_BACKEND" in source
    assert "import PyQt6" not in source
    assert "from PyQt6" not in source
