"""
tests/test_camera_diagnosis.py — offline tests for the webcam failure
diagnosis in actions/camera_watch.py (diagnose_camera / _camera_status_text).

No /dev/video*, no journalctl, no real sysfs needed: every function takes
injectable roots. The wedged-webcam scenario mirrors this laptop's real
failure (Inspiron 3542, Integrated_Webcam_HD wedged at the USB level with
"device descriptor read/64, error -32").

Run:  .venv/bin/python -m pytest tests/test_camera_diagnosis.py -q
"""

import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from actions.camera_watch import (  # noqa: E402
    diagnose_camera,
    _camera_status_text,
    _find_usb_device,
)


def _mk_fs(root: Path, files: dict[str, str]) -> str:
    """Materialise a fake /sys (or /dev) tree, return its path as string."""
    for rel, content in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    return str(root)


# ── 1. camera present → no diagnosis ────────────────────────────────────────
def test_no_diagnosis_when_camera_present(tmp_path):
    dev = _mk_fs(tmp_path / "dev", {"video0": ""})
    sysf = _mk_fs(tmp_path / "sys", {})
    assert diagnose_camera(dev, sysf, known_internal=True) == ""
    assert "Connected camera(s)" in _camera_status_text(dev, sysf, True)


# ── 2. wedged camera → power-drain advice ───────────────────────────────────
# Machine is known to have an internal cam (journal proof), controller alive,
# but the 0c45:670b device is NOT on the bus → the wedged case.
def test_wedged_camera_gets_power_drain_advice(tmp_path):
    dev = _mk_fs(tmp_path / "dev", {})  # no /dev/video*
    sysf = _mk_fs(
        tmp_path / "sys",
        {
            "bus/usb/devices/usb1/modalias": "",  # controllers present
            "bus/usb/devices/usb2/modalias": "",
            "bus/usb/devices/1-1.6/modalias": "usb:v0CF3p0036d...",  # BT only
        },
    )
    d = diagnose_camera(dev, sysf, known_internal=True)
    assert d, "wedged case must produce a diagnosis"
    assert "shutdown" in d and "charger" in d
    assert "power button" in d
    assert "external USB" in d  # workaround mentioned

    txt = _camera_status_text(dev, sysf, True)
    assert "No webcam is available right now." in txt
    assert "shutdown" in txt


# ── 3. half-enumerated camera → driver advice ───────────────────────────────
def test_half_enumerated_camera_gets_driver_advice(tmp_path):
    dev = _mk_fs(tmp_path / "dev", {})
    sysf = _mk_fs(
        tmp_path / "sys",
        {
            "bus/usb/devices/usb1/modalias": "",
            "bus/usb/devices/1-1.5/modalias": "usb:v0C45p670Bd32.62",  # cam, no node
        },
    )
    d = diagnose_camera(dev, sysf, known_internal=True)
    assert "modprobe uvcvideo" in d


# ── 4. machine never had an internal cam → generic advice, no diagnosis ─────
def test_no_internal_camera_generic_message(tmp_path):
    dev = _mk_fs(tmp_path / "dev", {})
    sysf = _mk_fs(
        tmp_path / "sys",
        {
            "bus/usb/devices/usb1/modalias": "",
        },
    )
    assert diagnose_camera(dev, sysf, known_internal=False) == ""
    txt = _camera_status_text(dev, sysf, False)
    assert txt == (
        "No webcam is connected right now. "
        "Plug one in and I'll detect it automatically."
    )


# ── 5. VM (no USB topology visible) → stay silent ───────────────────────────
def test_vm_silent(tmp_path):
    dev = _mk_fs(tmp_path / "dev", {})
    sysf = _mk_fs(tmp_path / "sys", {})  # no bus/usb/devices/usb*
    assert diagnose_camera(dev, sysf, known_internal=None) == ""


# ── 6. modalias matching is case-insensitive and exact on 4-hex fields ──────
def test_find_usb_device(tmp_path):
    sysf = _mk_fs(
        tmp_path / "sys",
        {
            "bus/usb/devices/1-1.5/modalias": "usb:v0C45p670Bd32.62",
            "bus/usb/devices/1-1.6/modalias": "usb:v0CF3p00361d...",  # near-miss
            "bus/usb/devices/1-1.8/modalias": "usb:v0BDAp0129d39.60",
        },
    )
    assert _find_usb_device("0c45:670b", str(sysf)) is True
    assert _find_usb_device("0cf3:0036", str(sysf)) is True
    assert _find_usb_device("0cf3:00361", str(sysf)) is False  # prefix no match
    assert _find_usb_device("1234:5678", str(sysf)) is False


# ── 6b. USB autosuspend warning (the flap cause) ────────────────────────────
def test_autosuspend_warning_when_control_auto(tmp_path):
    from actions.camera_watch import _autosuspend_warning, _camera_status_text

    dev = _mk_fs(tmp_path / "dev", {"video0": ""})
    sysf = _mk_fs(
        tmp_path / "sys",
        {
            "bus/usb/devices/1-1.5/idVendor": "0c45",
            "bus/usb/devices/1-1.5/idProduct": "670b",
            "bus/usb/devices/1-1.5/power/control": "auto\n",
        },
    )
    warn = _autosuspend_warning(str(sysf))
    assert "autosuspend" in warn
    assert "webcam-no-autosuspend.rules" in warn
    # It reaches the user through camera_status too.
    assert "autosuspend" in _camera_status_text(dev, sysf, True)


def test_no_autosuspend_warning_when_control_on(tmp_path):
    from actions.camera_watch import _autosuspend_warning

    sysf = _mk_fs(
        tmp_path / "sys",
        {
            "bus/usb/devices/1-1.5/idVendor": "0c45",
            "bus/usb/devices/1-1.5/idProduct": "670b",
            "bus/usb/devices/1-1.5/power/control": "on\n",
        },
    )
    assert _autosuspend_warning(str(sysf)) == ""


# ── 7. never raises (production contract) ───────────────────────────────────
def test_diagnose_never_raises(tmp_path):
    # roots that don't exist at all: must return a string, never raise.
    # (known_internal=True + no sysfs → falls to the wedged message, which is
    # correct: caller asserted an internal cam exists and none was found.)
    d = diagnose_camera("/nonexistent/dev", "/nonexistent/sys", known_internal=True)
    assert isinstance(d, str)
    assert (
        diagnose_camera("/nonexistent/dev", "/nonexistent/sys", known_internal=False)
        == ""
    )
    # VM-style: no USB topology visible + no forced answer → silent
    assert (
        diagnose_camera("/nonexistent/dev", "/nonexistent/sys", known_internal=None)
        == ""
    )
    # camera_status composes over broken roots without raising
    txt = _camera_status_text("/nonexistent/dev", "/nonexistent/sys", True)
    assert isinstance(txt, str) and txt


if __name__ == "__main__":
    # pytest-compatible: allow `python tests/test_camera_diagnosis.py`
    raise SystemExit(
        "Run with pytest: .venv/bin/python -m pytest "
        "tests/test_camera_diagnosis.py -q"
    )
