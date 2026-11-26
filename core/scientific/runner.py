"""
core/scientific/runner.py — bounded subprocess execution for analysis jobs.

This is the only code path that runs a child Python process for the science
agent. It is deliberately paranoid and deliberately small:

    * scrubbed environment (core.repo_agent.security.scrub_env): no JARVIS_*,
      no provider keys, no PYTHONPATH
    * HOME points at the job directory, so a script cannot read the user's
      ~/.ssh, ~/.aws or ~/.hermes; matplotlib gets a stable MPLCONFIGDIR
    * RLIMIT_AS / RLIMIT_CPU bound memory and CPU on a RAM-constrained machine
    * a hard wall clock, then SIGTERM → SIGKILL on the whole process group
    * OMP/MKL thread caps so one job cannot saturate the box
    * the child reports its own peak RSS (`ru_maxrss`), so no sampler thread
      outlives the run

There is no OS-level sandbox here (no namespace/container) — that is stated
plainly in the report rather than implied. What this does guarantee is that a
run cannot outlive its budget, cannot inherit secrets, and cannot write outside
its job directory by accident.
"""

from __future__ import annotations

import os
import signal
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from core.scientific.config import load_config

_WORKER = Path(__file__).resolve().parents[2] / "scripts" / "science_worker.py"
_TERM_GRACE_S = 5.0


@dataclass
class RunOutcome:
    ok: bool
    returncode: int
    stdout: str
    stderr: str
    duration_s: float
    timed_out: bool = False
    cancelled: bool = False
    peak_rss_mb: float = 0.0
    command: list[str] = field(default_factory=list)

    def public(self) -> dict:
        return {
            "ok": self.ok,
            "returncode": self.returncode,
            "duration_s": round(self.duration_s, 2),
            "timed_out": self.timed_out,
            "cancelled": self.cancelled,
            "peak_rss_mb": round(self.peak_rss_mb, 1),
            "stderr_tail": self.stderr[-1200:],
        }


# ── active process registry (cancellation) ─────────────────────────────────


class _Registry:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._procs: dict[str, list[subprocess.Popen]] = {}

    def attach(self, key: str, proc: subprocess.Popen) -> None:
        with self._lock:
            self._procs.setdefault(key, []).append(proc)

    def detach(self, key: str, proc: subprocess.Popen) -> None:
        with self._lock:
            procs = self._procs.get(key) or []
            if proc in procs:
                procs.remove(proc)
            if not procs:
                self._procs.pop(key, None)

    def kill(self, key: str) -> int:
        with self._lock:
            procs = list(self._procs.get(key) or [])
        killed = 0
        for proc in procs:
            if proc.poll() is None:
                _kill_group(proc)
                killed += 1
        return killed

    def active(self) -> dict[str, int]:
        with self._lock:
            return {key: len(procs) for key, procs in self._procs.items() if procs}


REGISTRY = _Registry()


def _kill_group(proc: subprocess.Popen) -> None:
    try:
        pgid = os.getpgid(proc.pid)
    except Exception:
        pgid = None
    try:
        if pgid is not None:
            os.killpg(pgid, signal.SIGTERM)
        else:
            proc.terminate()
    except Exception:
        pass
    try:
        proc.wait(timeout=_TERM_GRACE_S)
        return
    except Exception:
        pass
    try:
        if pgid is not None:
            os.killpg(pgid, signal.SIGKILL)
        else:
            proc.kill()
    except Exception:
        pass


def _child_env(job_dir: Path, memory_bytes: int = 0, cpu_seconds: int = 0) -> dict[str, str]:
    try:
        from core.repo_agent.security import scrub_env

        env = scrub_env()
    except Exception:
        env = {
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "LANG": "C.UTF-8",
            "PYTHONUNBUFFERED": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
        }
    cache = load_config().state_dir / "mpl_cache"
    cache.mkdir(parents=True, exist_ok=True)
    env.update({
        "RIO_RLIMIT_AS": str(int(memory_bytes or 0)),
        "RIO_RLIMIT_CPU": str(int(cpu_seconds or 0)),
        "HOME": str(job_dir),
        "MPLBACKEND": "Agg",
        "MPLCONFIGDIR": str(cache),
        "OMP_NUM_THREADS": "2",
        "MKL_NUM_THREADS": "2",
        "OPENBLAS_NUM_THREADS": "2",
        "PYTHONHASHSEED": "0",
        "TMPDIR": str(job_dir),
    })
    return env


def execute_worker(
    *,
    python: Path,
    recipe: str,
    params_path: Path,
    out_dir: Path,
    task_id: str = "",
    timeout_s: float | None = None,
    memory_bytes: int | None = None,
    worker: Path | None = None,
) -> RunOutcome:
    """Run one recipe in a bounded child process. Never raises."""
    cfg = load_config()
    timeout = float(timeout_s or cfg.run_timeout_s)
    memory = int(memory_bytes or cfg.run_rlimit_bytes)
    command = [
        str(python), str(worker or _WORKER),
        "--recipe", str(recipe),
        "--params", str(params_path),
        "--out", str(out_dir),
    ]
    started = time.monotonic()
    try:
        proc = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=str(out_dir),
            env=_child_env(out_dir, memory, int(timeout) + 10),
            # A new session gives us a process group to kill. The rlimits are
            # applied by the worker itself: preexec_fn forks with threads still
            # alive, which is documented as unsafe and segfaulted this
            # interpreter at shutdown.
            start_new_session=True,
        )
    except Exception as exc:
        return RunOutcome(False, -1, "", f"{type(exc).__name__}: {exc}",
                          time.monotonic() - started, command=command)

    if task_id:
        REGISTRY.attach(task_id, proc)

    timed_out = False
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        _kill_group(proc)
        try:
            stdout, stderr = proc.communicate(timeout=_TERM_GRACE_S)
        except Exception:
            stdout, stderr = "", "timed out and could not be reaped"
    finally:
        if task_id:
            REGISTRY.detach(task_id, proc)

    duration = time.monotonic() - started
    cancelled = proc.returncode in (-9, -15) and not timed_out
    return RunOutcome(
        ok=(proc.returncode == 0 and not timed_out),
        returncode=int(proc.returncode if proc.returncode is not None else -1),
        stdout=stdout or "",
        stderr=stderr or "",
        duration_s=duration,
        timed_out=timed_out,
        cancelled=cancelled,
        peak_rss_mb=0.0,
        command=command,
    )


def kill_task(task_id: str) -> int:
    """Cancel every child process belonging to one agent task."""
    return REGISTRY.kill(task_id)


def active_tasks() -> dict[str, int]:
    return REGISTRY.active()


def worker_exists() -> bool:
    return _WORKER.exists()
