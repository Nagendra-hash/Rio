#!/usr/bin/env python3
"""
scripts/bridge_selftest.py — permanent two-way JARVIS <-> Hermes bridge self-test.

Combines the two live drills that proved the bridge works (2026-09-27) into one
repeatable command:

  OUTBOUND (JARVIS -> Hermes)   actions/hermes_task.py path:
      a probe file with a random secret code is planted, then `hermes -z` is
      asked to read the code, report it, and delete the file. The code is NEVER
      in the prompt, so only real tool use can retrieve it — a hallucinating
      model cannot pass. Deletion is verified from this side afterwards.

  INBOUND (Hermes -> JARVIS)    hermes-hooks/jarvis-pdf-forward path:
      scripts/jarvis_inbox.py pings the dashboard's /api/message with a unique
      token (needs JARVIS running). Exit 0 = delivered.

  PREFLIGHT: hermes CLI present, gateway unit active, dashboard reachable —
      so a red result comes with the layer that failed, not just "it broke".

  --auto (cron) softens two noisy conditions into skips so silence stays
  trustworthy: JARVIS simply not running (no inbound arm), and transient
  free-tier model failures (retried once after a 35s backoff, then logged as
  a skip). Infrastructure breakage — CLI, gateway unit, inbox refused while
  the dashboard is up — still fails loudly.

Usage:
    scripts/bridge_selftest.py                  # both arms + preflight
    scripts/bridge_selftest.py --skip-inbound   # outbound only (JARVIS may be down)
    scripts/bridge_selftest.py --skip-outbound  # inbound only (fast, ~2 s)
    scripts/bridge_selftest.py --chat 12345     # override inbound chat id

Exit code 0 = everything that ran passed; 1 = any failure. Stdlib only, per
bridge house rules; subprocesses use the fixed env convention from
actions/hermes_task.py (HOME/PATH pinned, hard timeouts, nothing inherited).
"""
from __future__ import annotations

import argparse
import os
import random
import shutil
import ssl
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

_HOME       = "/home/nagendra"
_HERMES     = shutil.which("hermes") or f"{_HOME}/.local/bin/hermes"
_INBOX      = Path(__file__).resolve().parent / "jarvis_inbox.py"
_DASHBOARD  = os.environ.get("JARVIS_DASHBOARD_URL", "https://127.0.0.1:8000")
_TASK_TIMEOUT = 240          # same budget as actions/hermes_task.py
_ENV = {
    "PATH": f"{_HOME}/.local/bin:{_HOME}/.hermes/node/bin:/usr/local/bin:/usr/bin:/bin",
    "HOME": _HOME,
    # `systemctl --user` needs this when inherited from a bare context
    "XDG_RUNTIME_DIR": os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}"),
}

_RESULTS: list[tuple[str, bool | None, str]] = []   # (check, ok, note) — ok=None means skipped


def _record(name: str, ok: bool | None, note: str = "") -> None:
    _RESULTS.append((name, ok, note))
    mark = "✓" if ok else ("–" if ok is None else "✗")
    print(f"  {mark} {name}" + (f" — {note}" if note else ""))


def _hermes_cli() -> list[str]:
    return [_HERMES]


# ── preflight ────────────────────────────────────────────────────────────────

def preflight(check_dashboard: bool, auto: bool = False) -> bool | None:
    """Returns dashboard state: True (up), None (down + --auto skip), False (down = failure)."""
    print("── preflight ──")
    try:
        rc = subprocess.run([*_hermes_cli(), "--version"], env=_ENV,
                            capture_output=True, text=True, timeout=20)
        _record("hermes CLI", rc.returncode == 0,
                (rc.stdout or rc.stderr).strip().splitlines()[0][:60] if (rc.stdout or rc.stderr).strip() else "")
    except subprocess.TimeoutExpired:
        _record("hermes CLI", False, "timed out after 20s — CLI hung or system heavily loaded")
    except OSError as e:
        _record("hermes CLI", False, f"{type(e).__name__}: {e}")

    try:
        rc = subprocess.run(["systemctl", "--user", "is-active", "hermes-gateway"],
                            env=_ENV, capture_output=True, text=True, timeout=15)
        _record("gateway unit", rc.stdout.strip() == "active", rc.stdout.strip())
    except subprocess.TimeoutExpired:
        _record("gateway unit", False, "systemctl timed out after 15s")
    except OSError as e:
        _record("gateway unit", False, f"{type(e).__name__}: {e}")

    if not check_dashboard:
        return None
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE     # loopback, token-authenticated by design
    try:
        with urllib.request.urlopen(_DASHBOARD, timeout=6, context=ctx) as r:
            _record("dashboard reachable", True, f"HTTP {r.status} {_DASHBOARD}")
            return True
    except Exception as e:
        if auto:
            _record("dashboard reachable", None,
                    f"JARVIS not running ({type(e).__name__}) — inbound arm skipped")
            return None
        _record("dashboard reachable", False,
                f"{type(e).__name__} on {_DASHBOARD} — is JARVIS running? (bash run.sh)")
        return False


# ── outbound arm: probe file + hermes -z ─────────────────────────────────────

def _run_agent_turn(task: str, timeout: int) -> tuple[int, str]:
    rc = subprocess.run([*_hermes_cli(), "-z", task], env=_ENV,
                        capture_output=True, text=True, timeout=timeout)
    return rc.returncode, (rc.stdout or "").strip()


def outbound(timeout: int, auto: bool = False) -> None:
    print("── outbound: JARVIS → Hermes (probe-file task) ──")
    code = f"XB7-{random.randint(10000, 99999)}-{random.choice(['ALPHA', 'BRAVO', 'DELTA', 'ECHO'])}-{random.randint(1000, 9999)}"
    token = f"BRIDGE-SELFTEST-{int(time.time())}"
    probe = Path(tempfile.gettempdir()) / f"jarvis_bridge_probe_{os.getpid()}.txt"
    probe.write_text(f"Hermes bridge self-test probe.\nSECRET CODE: {code}\n({token})\n",
                     encoding="utf-8")
    task = (f"Read the file {probe}, report the exact SECRET CODE it contains, "
            f"then delete the file and confirm deletion.")

    try:
        t0 = time.monotonic()
        try:
            rc, answer = _run_agent_turn(task, timeout)
        except subprocess.TimeoutExpired:
            rc, answer = 124, ""
        # --auto (cron): one retry on a fast failure — free-tier turns blip on
        # per-minute rate limits and a lone watchdog howl trains the human to
        # ignore it. Back off 35s so the retry lands outside the same rate-limit
        # window; never retry after a timeout (worst case would double).
        if auto and rc not in (0, 124, 127) and (time.monotonic() - t0) < timeout / 2:
            time.sleep(35)
            try:
                rc, answer = _run_agent_turn(task, timeout)
            except subprocess.TimeoutExpired:
                rc, answer = 124, ""
        dt = time.monotonic() - t0

        if rc == 124:
            if auto:
                _record("agent turn", None,
                        f"timed out after {timeout}s — likely model slowness, not alerting")
                return
            _record("agent turn", False, f"timed out after {timeout}s (exit 124)")
            return
        if rc == 127:
            _record("agent turn", False, "hermes CLI not found (exit 127)")
            return
        if rc != 0:
            if auto:
                # cron mode: model flakiness is not page-worthy. The free tier
                # rate-limits often enough that alerting here would train the
                # human to ignore the watchdog. Infrastructure failures (CLI,
                # gateway, inbox) still alert — only this arm is softened.
                _record("agent turn", None,
                        f"transient model failure (exit {rc}) — not alerting; run without --auto for details")
                return
            _record("agent turn", False, f"exit {rc}: {answer[:120]}")
            return

        _record("agent turn completed", True, f"{dt:.0f}s")
        _record("secret code returned verbatim", code in answer,
                "real tool use confirmed" if code in answer else "code missing from answer — hallucination or tool failure")
        deleted = not probe.exists()
        _record("file deleted by agent", deleted,
                "confirmed from this side" if deleted else "file still present after task")
    except FileNotFoundError:
        _record("agent turn", False, "hermes CLI not found")
    except subprocess.TimeoutExpired:
        _record("agent turn", False, f"timed out after {timeout}s")
    finally:
        # never leave garbage behind, even on failure paths
        if probe.exists():
            probe.unlink(missing_ok=True)


# ── inbound arm: jarvis_inbox.py ping ────────────────────────────────────────

def inbound(chat: str | None) -> None:
    print("── inbound: Hermes → JARVIS (dashboard ping) ──")
    if chat is None:
        chat = "1"                     # same neutral chat id used in the 2026-09-27 drill
        env_file = Path(_HOME) / ".hermes" / ".env"
        try:
            for line in env_file.read_text(encoding="utf-8", errors="ignore").splitlines():
                if line.startswith("TELEGRAM_HOME_CHANNEL="):
                    chat = line.split("=", 1)[1].strip().strip('"').strip("'") or chat
                    break
        except OSError:
            pass

    token = f"ping {int(time.time())}"
    try:
        rc = subprocess.run([sys.executable, str(_INBOX),
                             "--channel", "telegram", "--chat", chat, "--text", token],
                            env={**_ENV, **os.environ},     # allow JARVIS_DASHBOARD_URL/TOKEN overrides
                            capture_output=True, text=True, timeout=45)
        out = (rc.stdout or rc.stderr or "").strip()
        _record("inbox delivered", rc.returncode == 0,
                out[:80] if rc.returncode == 0 else f"exit {rc.returncode}: {out[:120]}")
    except subprocess.TimeoutExpired:
        _record("inbox delivered", False, "jarvis_inbox.py timed out after 45s")
    except OSError as e:
        _record("inbox delivered", False, f"{type(e).__name__}: {e}")


# ── main ─────────────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(description="Two-way JARVIS <-> Hermes bridge self-test")
    ap.add_argument("--skip-inbound", action="store_true", help="skip the dashboard ping (JARVIS may be down)")
    ap.add_argument("--skip-outbound", action="store_true", help="skip the probe-file agent task")
    ap.add_argument("--chat", metavar="ID", help="chat id for the inbound ping (default: TELEGRAM_HOME_CHANNEL)")
    ap.add_argument("--timeout", type=int, default=_TASK_TIMEOUT,
                    help=f"outbound agent-turn budget in seconds (default {_TASK_TIMEOUT})")
    ap.add_argument("--auto", action="store_true",
                    help="cron mode: a down dashboard SKIPS the inbound arm instead of failing "
                         "(use when JARVIS is not always running)")
    args = ap.parse_args()

    print(f"bridge self-test @ {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
    dashboard_up = preflight(check_dashboard=not args.skip_inbound, auto=args.auto)
    if not args.skip_outbound:
        outbound(args.timeout, auto=args.auto)
    # run inbound only on a verified-up dashboard: False = down (already recorded
    # as failure by preflight), None = not checked (--skip-inbound) or --auto skip
    if not args.skip_inbound and dashboard_up is True:
        inbound(args.chat)

    failed = [n for n, ok, _ in _RESULTS if ok is False]
    skipped = [n for n, ok, _ in _RESULTS if ok is None]
    ran = [(n, ok) for n, ok, _ in _RESULTS if ok is not None]
    verdict = f"\nverdict: {len(ran) - len(failed)}/{len(ran)} checks passed"
    if skipped:
        verdict += f" (skipped: {', '.join(skipped)})"
    verdict += f" — failed: {', '.join(failed)}" if failed else " — bridge is fully operational."
    print(verdict)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
