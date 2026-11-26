"""
tests/test_rotation_smoke.py — regression tests for systemd/jarvis-rotation-smoke.sh

The smoke script's whole brain is one awk classifier over a journald window.
Rather than mock bash or journalctl, the script exposes that classifier via:

    systemd/jarvis-rotation-smoke.sh --analyze   (window body on stdin)

so every test below drives the EXACT code path a real 5-minute run uses.

Fixtures are built from the real log grammar observed in the journal:

  [JARVIS] 🔔 GoAway received — reconnecting proactively
  [JARVIS] Voluntary reconnect requested.
  [JARVIS] Connected.
  [JARVIS] ❌ Recv: 1008 None. The operation was aborted.   (2026-09-28 incident)
  [JARVIS] 🔗 Resumption handle rejected — starting a fresh session

Run:  python3 tests/test_rotation_smoke.py   (or pytest)

Exit codes: 0 = pass, 1 = regression.
"""

import os
import subprocess
import sys
import tempfile
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
SCRIPT = BASE / "systemd" / "jarvis-rotation-smoke.sh"

SCRIPT_HEADER = "#!/usr/bin/env bash\n# test stub\n"

# ── journal line fixtures (the real grammar) ─────────────────────────────────

CLEAN_ROTATION = "\n".join(
    [
        "Sep 28 03:45:13 host jarvis_gui_wrapper.sh[1]: [JARVIS] 🔔 GoAway received — reconnecting proactively",
        "Sep 28 03:45:13 host jarvis_gui_wrapper.sh[1]: [JARVIS] Voluntary reconnect requested.",
        "Sep 28 03:45:15 host jarvis_gui_wrapper.sh[1]: [JARVIS] Connected.",
    ]
)

# The exact incident this guards against: recv-path 1008 abort → traceback
ABORT_ROTATION = "\n".join(
    [
        "Sep 28 03:45:13 host jarvis_gui_wrapper.sh[1]: [JARVIS] 🔔 GoAway received — will reconnect after this turn",
        "Sep 28 03:45:14 host jarvis_gui_wrapper.sh[1]: [JARVIS] ❌ Recv: 1008 None. The operation was aborted.",
        "Sep 28 03:45:14 host jarvis_gui_wrapper.sh[1]: Traceback (most recent call last):",
        "Sep 28 03:45:14 host jarvis_gui_wrapper.sh[1]: google.genai.errors.APIError: 1008 None. The operation was aborted.",
        "Sep 28 03:45:20 host jarvis_gui_wrapper.sh[1]: [JARVIS] Connected.",
    ]
)

# The 2026-09-27 poison-handle loop: rejection lines OUTSIDE any rotation
HANDLE_REJECTED = "\n".join(
    [
        "Sep 27 09:00:01 host jarvis_gui_wrapper.sh[1]: [JARVIS] 🔗 Resumption handle rejected — starting a fresh session",
    ]
)

# 2026-09-28 steady state (root cause: upstream server reaps content-bearing
# session handles — see scripts/diag_*): ONE rejection inside a rotation that
# then completes via the fresh fallback. Degraded, handled, NOT an incident.
REJECTED_ONCE_ROTATION = "\n".join(
    [
        "Sep 28 13:50:16 host jarvis_gui_wrapper.sh[1]: [JARVIS] 🔔 GoAway received — reconnecting proactively",
        "Sep 28 13:50:17 host jarvis_gui_wrapper.sh[1]: [JARVIS] 🔗 Resumption handle rejected — starting a fresh session (1008 None. BidiGenerateContent session not found) [sent handle: 6662d32c-cdc5-…]",
        "Sep 28 13:50:17 host jarvis_gui_wrapper.sh[1]: [JARVIS] 🔌 connect: api_version=v1alpha, handle=None (fresh)",
        "Sep 28 13:50:18 host jarvis_gui_wrapper.sh[1]: [JARVIS] Connected.",
    ]
)

# Regression alarm: TWO rejections inside one rotation = rejection loop
REJECT_LOOP_ROTATION = "\n".join(
    [
        "Sep 28 13:50:16 host jarvis_gui_wrapper.sh[1]: [JARVIS] 🔔 GoAway received — reconnecting proactively",
        "Sep 28 13:50:17 host jarvis_gui_wrapper.sh[1]: [JARVIS] 🔗 Resumption handle rejected — starting a fresh session (1008 None. BidiGenerateContent session not found) [sent handle: 6662d32c-cdc5-…]",
        "Sep 28 13:50:19 host jarvis_gui_wrapper.sh[1]: [JARVIS] 🔗 Resumption handle rejected — starting a fresh session (1008 None. BidiGenerateContent session not found) [sent handle: deadbeef-1234-…]",
        "Sep 28 13:50:20 host jarvis_gui_wrapper.sh[1]: [JARVIS] Connected.",
    ]
)

# A TOOL traceback (browser_control prints its own) — must NOT trip the check
TOOL_TRACEBACK = "\n".join(
    [
        "Sep 28 04:10:01 host jarvis_gui_wrapper.sh[1]: [Browser] clicking button",
        "Sep 28 04:10:02 host jarvis_gui_wrapper.sh[1]: Traceback (most recent call last):",
        "Sep 28 04:10:02 host jarvis_gui_wrapper.sh[1]: playwright._impl._errors.TimeoutError",
    ]
)

SEND_LOOP = "\n".join(
    [
        "Sep 28 05:00:00 host jarvis_gui_wrapper.sh[1]: [JARVIS] Same connect error repeating — retrying in 6s",
    ]
)


def _analyze(window: str) -> tuple[int, str]:
    """Run the script's --analyze hook on a window body. Returns (rc, stdout)."""
    p = subprocess.run(
        ["bash", str(SCRIPT), "--analyze"],
        input=window + "\n",
        capture_output=True,
        text=True,
        timeout=30,
    )
    return p.returncode, p.stdout.strip()


# ── tests ────────────────────────────────────────────────────────────────────


def test_clean_rotation_passes():
    rc, out = _analyze(CLEAN_ROTATION)
    assert rc == 0, f"clean rotation flagged: {out!r}"
    assert "rotations=1 completed=1" in out
    assert "OK" in out


def test_1008_abort_inside_rotation_fails():
    """CORE: the 2026-09-28 incident shape must be caught."""
    rc, out = _analyze(ABORT_ROTATION)
    assert rc == 1, f"abort rotation passed the check: {out!r}"
    assert "❌ Recv: 1008 None. The operation was aborted." in out
    assert "google.genai.errors" in out


def test_handle_rejection_outside_rotation_fails():
    rc, out = _analyze(HANDLE_REJECTED)
    assert rc == 1, f"handle rejection passed the check: {out!r}"
    assert "handle rejection" in out


def test_single_rejection_in_completed_rotation_is_degraded_not_fail():
    """2026-09-28 steady state: the upstream Live API bug reaps handles of
    content-bearing sessions, so every rotation 2+ falls back to a fresh
    session (with the conversation tail carried over). ONE rejection inside a
    rotation that completes = DEGRADED, exit 0 — no page."""
    rc, out = _analyze(REJECTED_ONCE_ROTATION)
    assert rc == 0, f"single-rejection rotation flagged: {out!r}"
    assert "rotations=1 completed=1" in out
    assert "DEGRADED" in out
    assert "FAIL" not in out


def test_rejection_loop_within_one_rotation_fails():
    """Two or more rejections inside ONE rotation is a loop — must page."""
    rc, out = _analyze(REJECT_LOOP_ROTATION)
    assert rc == 1, f"rejection loop passed the check: {out!r}"
    assert "rejection loop" in out


def test_mixed_rotation_with_recv_error_still_fails():
    """A rejection does not mask real errors: recv/traceback lines inside a
    rotation keep failing it even when a rejection is also present."""
    window = (
        ABORT_ROTATION
        + "\n"
        + (
            "Sep 28 03:45:16 host jarvis_gui_wrapper.sh[1]: [JARVIS] 🔗 Resumption handle rejected — starting a fresh session"
        )
    )
    rc, out = _analyze(window)
    assert rc == 1, f"recv-error rotation passed the check: {out!r}"
    assert "❌ Recv: 1008" in out


def test_tool_traceback_does_not_fail():
    """An action's own traceback is routine noise — the smoke check must
    stay quiet about it (session-scoped markers only)."""
    rc, out = _analyze(TOOL_TRACEBACK)
    assert rc == 0, f"tool traceback flagged: {out!r}"
    assert "no rotation in window" in out


def test_send_error_loop_fails():
    rc, out = _analyze(SEND_LOOP)
    assert rc == 1, f"send-error loop passed the check: {out!r}"


def test_gaway_without_reconnect_is_incomplete():
    """A rotation window that never completes must fail even with zero
    exception lines — that is a dead session, the loudest alarm there is."""
    rc, out = _analyze(
        "Sep 28 06:00:00 host jarvis_gui_wrapper.sh[1]: [JARVIS] 🔔 GoAway received — will reconnect after this turn"
    )
    assert rc == 1, f"incomplete rotation passed the check: {out!r}"
    assert "INCOMPLETE" in out


def test_two_clean_rotations_counted():
    rc, out = _analyze(CLEAN_ROTATION + "\n" + CLEAN_ROTATION)
    assert rc == 0, f"double rotation flagged: {out!r}"
    assert "rotations=2 completed=2" in out


def test_analyze_hook_reachable():
    """The script must exist, be executable, and its --analyze hook must not
    choke on empty input."""
    assert SCRIPT.is_file(), f"missing {SCRIPT}"
    import os

    assert os.access(SCRIPT, os.X_OK), f"not executable: {SCRIPT}"
    rc, out = _analyze("")
    assert rc == 0 and "no rotation" in out


# ── failure-notification path (--notify hook, stubbed channels) ─────────────


def _make_notify_env(tmp: Path) -> dict:
    """Env for --notify runs: all state in tmp, PATH fronted by a bin dir
    holding notify-send/curl stubs that record their argv."""
    env = dict(os.environ)
    env["JARVIS_SMOKE_LOG"] = str(tmp / "smoke.log")
    env["JARVIS_SMOKE_LASTFAIL_FILE"] = str(tmp / "lastfail")
    env["JARVIS_SMOKE_ENV_FILE"] = str(tmp / "hermes.env")
    env["JARVIS_SMOKE_NOTIFY_COOLDOWN"] = "60"
    env["PATH"] = f"{tmp / 'bin'}:{env.get('PATH', '')}"
    env["JARVIS_SMOKE_NS_LOG"] = str(tmp / "ns.log")
    env["JARVIS_SMOKE_CURL_LOG"] = str(tmp / "curl.log")
    return env


def _write_channel_stubs(tmp: Path) -> None:
    bindir = tmp / "bin"
    bindir.mkdir(exist_ok=True)
    for name, marker in (
        ("notify-send", "JARVIS_SMOKE_NS_LOG"),
        ("curl", "JARVIS_SMOKE_CURL_LOG"),
    ):
        stub = bindir / name
        stub.write_text(
            "#!/usr/bin/env bash\n"
            # one LOGICAL line per call: verdicts are multi-line, so fold the
            # embedded \n away — but re-add the trailing newline tr just ate,
            # or every append lands on the same physical line and per-call
            # line counts stop matching call counts
            f'[ -n "${{{marker}:-}}" ] && {{ echo "$*" | tr \'\\n\' \' \' >> "${{{marker}}}"; echo >> "${{{marker}}}"; }}\n'
            # curl must emulate -w "%{http_code}" like the real one
            + ("echo 200\n" if name == "curl" else "")
            + "exit 0\n"
        )
        stub.chmod(0o755)
    (tmp / "hermes.env").write_text(
        'TELEGRAM_BOT_TOKEN="TESTTOKEN"\nTELEGRAM_CHAT_ID="12345"\n'
    )


def _notify(tmp: Path, verdict: str, extra_env: dict | None = None) -> int:
    env = _make_notify_env(tmp)
    if extra_env:
        env.update(extra_env)
    p = subprocess.run(
        ["bash", str(SCRIPT), "--notify", verdict],
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
    )
    return p.returncode


VERDICT_A = (
    "rotations=1 completed=0\nFAIL: ❌ Recv: 1008 None. The operation was aborted."
)
VERDICT_B = "rotations=1 completed=0\nFAIL: FAIL: 2 resumption handle rejection(s) — conversation reset"


def test_notify_first_failure_pings_both_channels():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        _write_channel_stubs(tmp)
        rc = _notify(tmp, VERDICT_A)
        assert rc == 0, "notify path must never fail its caller"
        ns = (tmp / "ns.log").read_text()
        curl = (tmp / "curl.log").read_text()
        log = (tmp / "smoke.log").read_text()
        assert "urgency=critical" in ns and "rotation smoke" in ns
        assert "sendMessage" in curl and "chat_id=12345" in curl
        assert "TESTTOKEN" in curl, "bot token missing from sendMessage"
        assert "🖥️ desktop toast sent" in log and "📣 telegram http=200" in log


def test_notify_cooldown_suppresses_same_failure():
    """The failing window stays unpinned, so the same incident is re-seen
    every 5 min — the SECOND identical failure within the cooldown must be
    logged but NOT re-pinged."""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        _write_channel_stubs(tmp)
        _notify(tmp, VERDICT_A)
        _notify(tmp, VERDICT_A)
        ns1 = len((tmp / "ns.log").read_text().splitlines())
        curl1 = len((tmp / "curl.log").read_text().splitlines())
        assert (
            "🔕 smoke failure repeat" in (tmp / "smoke.log").read_text()
        ), f"cooldown did not suppress the repeat; log: {(tmp / 'smoke.log').read_text()!r}"
        # a DIFFERENT failure always pings
        _notify(tmp, VERDICT_B)
        ns2 = len((tmp / "ns.log").read_text().splitlines())
        curl2 = len((tmp / "curl.log").read_text().splitlines())
        assert ns2 == ns1 + 1 and curl2 == curl1 + 1


def test_notify_zero_cooldown_pings_every_run():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        _write_channel_stubs(tmp)
        _notify(tmp, VERDICT_A, {"JARVIS_SMOKE_NOTIFY_COOLDOWN": "0"})
        _notify(tmp, VERDICT_A, {"JARVIS_SMOKE_NOTIFY_COOLDOWN": "0"})
        assert len((tmp / "ns.log").read_text().splitlines()) == 2


def test_notify_silent_when_no_credentials():
    """No ~/.hermes/.env → no Telegram, no crash; the desktop toast still
    fires. A notification channel must never break the health check."""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        _write_channel_stubs(tmp)
        (tmp / "hermes.env").unlink()  # creds file absent
        rc = _notify(tmp, VERDICT_A)
        assert rc == 0
        assert "urgency=critical" in (tmp / "ns.log").read_text()
        assert (tmp / "curl.log").exists() is False
        assert "📣" not in (tmp / "smoke.log").read_text()


# ── standalone runner ────────────────────────────────────────────────────────


def run_tests() -> int:
    tests = [
        test_clean_rotation_passes,
        test_1008_abort_inside_rotation_fails,
        test_handle_rejection_outside_rotation_fails,
        test_single_rejection_in_completed_rotation_is_degraded_not_fail,
        test_rejection_loop_within_one_rotation_fails,
        test_mixed_rotation_with_recv_error_still_fails,
        test_tool_traceback_does_not_fail,
        test_send_error_loop_fails,
        test_gaway_without_reconnect_is_incomplete,
        test_two_clean_rotations_counted,
        test_analyze_hook_reachable,
        test_notify_first_failure_pings_both_channels,
        test_notify_cooldown_suppresses_same_failure,
        test_notify_zero_cooldown_pings_every_run,
        test_notify_silent_when_no_credentials,
    ]
    failed = 0
    for t in tests:
        try:
            t()
        except Exception as e:  # noqa: BLE001 — report, then continue
            failed += 1
            print(f"FAIL  {t.__name__}: {type(e).__name__}: {e}")
        else:
            print(f"PASS  {t.__name__}")
    print("REGRESSION" if failed else "ALL GREEN")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(run_tests())
