"""
tests/test_live_model_watch.py — regression tests for systemd/jarvis-live-model-watch.sh

The watch script's brain is: read live_model → probe the CANDIDATE via
scripts/diag_live_speech_health.py --probe --json → (broken | streak | flip |
rollback) → append a machine-readable record to memory/incidents.jsonl.

Rather than hitting the network or systemd, every test drives the EXACT code
path a real 30-minute timer run uses, through the script's own JARVIS_LMW_*
seams: the probe is a stub that emits the real probe's output grammar
(chatter line + one JSON verdict), and systemctl is a stub reporting
jarvis-gui active/inactive.

The incident-JSONL contract under test (born 2026-10-01, after the manual
timeline was kept in memory/incidents.jsonl during the 2026-09-30 Live-voice
incident):
  * every run appends ZERO or ONE record, one JSON object per line
  * records carry: event, id, model, at, probe (parsed --json verdict)
  * probe chatter must never leak into the JSONL
  * JARVIS_LMW_INCIDENTS="" disables logging (opt-out, not default-fallback)
  * logging must never break the probe cycle (best-effort by design)

Run:  python3 tests/test_live_model_watch.py   (or pytest)

Exit codes: 0 = pass, 1 = regression.
"""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
SCRIPT = BASE / "systemd" / "jarvis-live-model-watch.sh"

FAILOVER = "models/gemini-2.5-flash-native-audio-latest"   # JARVIS rides this
CANDIDATE = "models/gemini-3.1-flash-live-preview"          # watchdog probes this

# ── stubs (the real probe's output grammar: chatter line, then ONE JSON) ─────

STUB_PROBE = r"""#!/usr/bin/env bash
# Emulates scripts/diag_live_speech_health.py --probe --json. STUB_VERDICT
# (inherited env) selects healthy|broken. The chatter line exercises the
# watch script's last-JSON-line parsing and its chatter filter.
echo "stub probe chatter (noise before the JSON verdict)"
case "${STUB_VERDICT:-broken}" in
  healthy)
    echo '{"model": "models/gemini-3.1-flash-live-preview", "model_turn": true, "t_transcribe": 4.2, "t_model_turn": 6.5, "t_reply": 7.1, "checked_at": "2026-10-01T19:30:00"}'
    exit 0 ;;
  *)
    echo "  => FAILED 75s: TimeoutError: (stub)"
    echo '{"model": "models/gemini-3.1-flash-live-preview", "model_turn": false, "t_transcribe": null, "t_model_turn": null, "t_reply": null, "checked_at": "2026-10-01T19:30:00"}'
    exit 1 ;;
esac
"""

STUB_SYSCTL_ACTIVE = """#!/usr/bin/env bash
# jarvis-gui always comes back healthy after a flip.
[ "$2" = "is-active" ] && echo active
exit 0
"""

STUB_SYSCTL_INACTIVE = """#!/usr/bin/env bash
# jarvis-gui never comes back — forces the rollback path.
[ "$2" = "is-active" ] && echo inactive
exit 0
"""


# ── harness ──────────────────────────────────────────────────────────────────

class Bench:
    """Scratch HOME + config + incident log for one test; runs the REAL script."""

    def __init__(self, root: Path):
        self.root = root
        self.home = root / "home"
        self.config = root / "config.json"
        self.incidents = root / "incidents.jsonl"
        self.watch_log = root / "watch.log"
        self.state = root / "state"
        self.bin = root / "bin"
        self.bin.mkdir(parents=True)
        (self.home / "Mark-LIII" / "memory").mkdir(parents=True)
        self.set_config(FAILOVER)
        self._write(self.bin / "stub_probe.sh", STUB_PROBE)
        self._write(self.bin / "sysctl_active.sh", STUB_SYSCTL_ACTIVE)
        self._write(self.bin / "sysctl_inactive.sh", STUB_SYSCTL_INACTIVE)

    @staticmethod
    def _write(path: Path, body: str) -> None:
        path.write_text(body)
        path.chmod(0o755)

    def set_config(self, model: str) -> None:
        self.config.write_text(json.dumps({"live_model": model}))

    def read_config(self) -> str:
        return json.loads(self.config.read_text())["live_model"]

    def records(self) -> list[dict]:
        """Parse the incident JSONL — also proves one JSON object per line."""
        if not self.incidents.exists():
            return []
        out = []
        for i, line in enumerate(self.incidents.read_text().splitlines(), 1):
            if line.strip():
                out.append(json.loads(line))  # raises if any line is not JSON
        return out

    def run(self, *, verdict: str, recovery_hits: int = 2,
            sysctl: str = "sysctl_active.sh", incidents: str | None = None,
            candidate: str = CANDIDATE) -> tuple[int, str]:
        """One timer-cycle equivalent. Returns (exit_code, watch_log_text)."""
        env = {
            "HOME": str(self.home),
            "PATH": "/usr/bin:/bin",                 # python3 lives here
            "STUB_VERDICT": verdict,                 # inherited by the stub probe
            "JARVIS_LMW_CONFIG": str(self.config),
            "JARVIS_LMW_STATE_FILE": str(self.state),
            "JARVIS_LMW_LOG": str(self.watch_log),
            "JARVIS_LMW_INCIDENTS": str(self.incidents) if incidents is None else incidents,
            "JARVIS_LMW_RECOVERY_HITS": str(recovery_hits),
            "JARVIS_LMW_CANDIDATE": candidate,
            "JARVIS_LMW_NOTIFY": "0",                # channels stubbed out
            "JARVIS_LMW_PROBE": f"bash {self.bin / 'stub_probe.sh'}",
            "JARVIS_LMW_SYSTEMCTL": str(self.bin / sysctl),
        }
        p = subprocess.run(
            ["bash", str(SCRIPT)], env=env,
            capture_output=True, text=True, timeout=60,
        )
        log = self.watch_log.read_text() if self.watch_log.exists() else ""
        return p.returncode, log


def _bench() -> Bench:
    # pytest passes tmp_path; the standalone runner passes a fresh tempdir.
    return Bench(Path(tempfile.mkdtemp(prefix="lmw_pytest_")))  # noqa: DTZ005


def test_broken_probe_appends_parsed_verdict():
    """CORE: the 2026-09-30/10-01 incident shape — every broken probe lands in
    the incident JSONL with the probe's own --json verdict parsed inside."""
    b = _bench()
    rc, log = b.run(verdict="broken")
    assert rc == 0, f"broken-probe cycle must stay exit 0: {log!r}"
    recs = b.records()
    assert len(recs) == 1, f"expected exactly 1 record: {recs!r}"
    rec = recs[0]
    assert rec["event"] == "probe_broken"
    assert rec["id"] == "live-20260930-3.1"
    assert rec["model"] == CANDIDATE
    assert rec["probe"]["model_turn"] is False
    assert rec["probe"]["checked_at"], "probe verdict fields missing"
    assert "T" in rec["at"], f"timestamp not ISO: {rec['at']!r}"


def test_probe_chatter_never_leaks_into_jsonl():
    """The stub emits a chatter line before the verdict — none of it may land
    in the incident log, and every line must stay parseable JSON."""
    b = _bench()
    b.run(verdict="broken")
    text = b.incidents.read_text()
    assert "chatter" not in text
    assert "TimeoutError" not in text          # stderr chatter, not the verdict
    assert all(line.lstrip().startswith("{") for line in text.splitlines() if line)


def test_steady_state_on_candidate_writes_no_records():
    """JARVIS already flipped back to the candidate → silent exit, zero spam."""
    b = _bench()
    b.set_config(CANDIDATE)
    rc, _ = b.run(verdict="healthy")
    assert rc == 0
    assert b.records() == []


def test_healthy_probe_records_streak_but_waits():
    """1/2 healthy: record the streak, do NOT flip, config untouched."""
    b = _bench()
    rc, log = b.run(verdict="healthy", recovery_hits=2)
    assert rc == 0
    recs = b.records()
    assert [r["event"] for r in recs] == ["probe_healthy"]
    assert recs[0]["streak"] == 1 and recs[0]["required"] == 2
    assert b.read_config() == FAILOVER, "flipped on a single healthy probe!"
    assert "waiting for more confirmations" in log


def test_two_healthy_probes_flip_and_record():
    """2/2 healthy: flip live_model to the candidate and record `recovered`.
    Also proves the log APPENDS across runs (streak record stays put)."""
    b = _bench()
    rc1, _ = b.run(verdict="healthy", recovery_hits=2)
    rc2, _ = b.run(verdict="healthy", recovery_hits=2)
    assert rc1 == 0 and rc2 == 0
    assert b.read_config() == CANDIDATE, "config was not flipped"
    events = [r["event"] for r in b.records()]
    assert events == ["probe_healthy", "recovered"], events
    recovered = b.records()[1]
    assert recovered["streak"] == 2 and recovered["required"] == 2
    assert recovered["flipped_to"] == CANDIDATE


def test_flip_rollback_recorded_and_config_restored():
    """Service dead after flip → rollback record + config restored, exit 1
    (the alarm surface, same convention as jarvis-rotation-smoke)."""
    b = _bench()
    rc, log = b.run(verdict="healthy", recovery_hits=1, sysctl="sysctl_inactive.sh")
    assert rc == 1, f"failed flip must exit 1: {log!r}"
    assert b.read_config() == FAILOVER, "rollback did not restore the config"
    recs = b.records()
    assert [r["event"] for r in recs] == ["flip_rolled_back"]
    assert recs[0]["rolled_back_to"] == FAILOVER
    assert "ROLLING BACK" in log


def test_empty_incidents_override_disables_logging():
    """JARVIS_LMW_INCIDENTS='' is an explicit OPT-OUT — dash-only default, so
    empty must disable logging, not silently fall back to the default path."""
    b = _bench()
    rc, _ = b.run(verdict="broken", incidents="")
    assert rc == 0
    assert not b.incidents.exists(), "empty override still created the file"


def test_logging_failure_never_breaks_the_probe_cycle():
    """An unwritable incident path must not change the exit code or flip
    behavior — logging is best-effort by design."""
    b = _bench()
    # A directory in place of the JSONL file → open(path, "a") fails.
    b.incidents.parent.mkdir(parents=True, exist_ok=True)
    b.incidents.mkdir()
    rc, _ = b.run(verdict="broken")
    assert rc == 0, "incident-log failure leaked into the probe cycle"
    rc2, _ = b.run(verdict="healthy", recovery_hits=1)   # flip still works
    assert rc2 == 0
    assert b.read_config() == CANDIDATE


def test_records_accumulate_across_mixed_outcomes():
    """A broken→healthy timeline appends in order, no truncation."""
    b = _bench()
    b.run(verdict="broken")
    b.run(verdict="healthy", recovery_hits=1)
    assert [r["event"] for r in b.records()] == ["probe_broken", "recovered"]


def test_script_present_and_parses():
    assert SCRIPT.is_file(), f"missing {SCRIPT}"
    import os
    assert os.access(SCRIPT, os.X_OK), f"not executable: {SCRIPT}"
    p = subprocess.run(["bash", "-n", str(SCRIPT)], capture_output=True, text=True)
    assert p.returncode == 0, f"bash -n failed: {p.stderr!r}"


# ── standalone runner ────────────────────────────────────────────────────────

def run_tests() -> int:
    tests = [
        test_broken_probe_appends_parsed_verdict,
        test_probe_chatter_never_leaks_into_jsonl,
        test_steady_state_on_candidate_writes_no_records,
        test_healthy_probe_records_streak_but_waits,
        test_two_healthy_probes_flip_and_record,
        test_flip_rollback_recorded_and_config_restored,
        test_empty_incidents_override_disables_logging,
        test_logging_failure_never_breaks_the_probe_cycle,
        test_records_accumulate_across_mixed_outcomes,
        test_script_present_and_parses,
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
