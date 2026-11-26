"""
core/browser_agent/runtime.py — the one client both entry points share.

`actions/browser_agent.py` (voice/dashboard) and
`orchestrator/agents/browser/browser_agent.py` (text/Telegram) both call this
module; neither talks to browser-use or a subprocess on its own. That is the
whole point: one implementation, one policy, one log.

SHAPE OF A RUN
    policy verdict  -> denied / needs_approval / allowed
    routing plan    -> tool (delegate) / http (requests+bs4) / browser
    execution       -> a daemon thread:
                         http  : fetch_static() inside a bounded requests call
                         browser: Popen(scripts/browser_use_run.py --json)
    result          -> BrowserRunResult, appended to memory/browser_agent/runs.jsonl

WHY A SUBPROCESS
    browser-use 1.0 runs a model-written Python REPL on its own event loop and
    can crash a browser. A subprocess gives crash isolation, a hard wall-clock
    budget, a killable process group (no orphan Chrome), and keeps the
    pydantic-ai dependency tree out of RIO's interpreter.

NOTHING BLOCKS THE VOICE LOOP
    `start()` returns in milliseconds with a job id; callers either poll
    `status()` or pass `wait=True` (the orchestrator path, which runs off the
    audio loop anyway).
"""

from __future__ import annotations

import collections
import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from core import outcome
from core.browser_agent import policy, router
from core.browser_agent.config import ALLOWED_BRAINS, BrowserAgentConfig, load_config

_RUNNER = Path(__file__).resolve().parents[2] / "scripts" / "browser_use_run.py"
_DEFAULT_VENV = Path.home() / ".jarvis-browser-venv" / "bin" / "python"
_TERM_GRACE_S = 6.0
# Chrome launch + model preflight happen before the agent's own clock starts,
# so the process gets this much extra before the wall clock is enforced.
_STARTUP_GRACE_S = 45.0

_preflight_lock = threading.Lock()
_preflight_cache: dict[str, tuple[bool, str, str]] = {}


# ── result ──────────────────────────────────────────────────────────────────


@dataclass
class BrowserRunResult:
    """One browser task's real outcome. `text` is what a caller may show."""

    job_id: str
    ok: bool
    status: str
    final: str = ""
    error: str = ""
    brain: str = ""
    model: str = ""
    steps: int = 0
    duration_s: float = 0.0
    downloads: tuple[str, ...] = ()
    injection_detected: bool = False
    captcha_detected: bool = False
    plan: dict = field(default_factory=dict)
    skipped_models: tuple[dict, ...] = ()
    task: str = ""
    text: str = ""

    def public(self) -> dict:
        return {
            "job_id": self.job_id,
            "ok": self.ok,
            "status": self.status,
            "brain": self.brain,
            "model": self.model,
            "steps": self.steps,
            "duration_s": self.duration_s,
            "downloads": list(self.downloads),
            "injection_detected": self.injection_detected,
            "captcha_detected": self.captcha_detected,
            "error": self.error,
        }

    def render(self, cfg: BrowserAgentConfig) -> str:
        """The caller-facing sentence(s), wrapped for the model."""
        if self.status == "denied":
            return outcome.failed("browser task", self.error or "refused")
        if self.status == "needs_approval":
            return outcome.ask(
                "browser task",
                f"{self.error}. Should I go ahead?",
                task=self.task[:180],
            )
        if self.status == "delegated":
            return self.error or "This request does not need a browser."
        if not self.ok:
            reason = self.error or self.status or "the task did not finish"
            return outcome.failed("browser task", reason)

        body = self.final.strip()
        if len(body) > cfg.max_result_chars:
            body = body[: cfg.max_result_chars].rstrip() + " …[truncated]"
        facts = [
            f"{self.brain}/{self.model}" if self.model else self.brain,
            f"{self.steps} step(s)",
            f"{self.duration_s:.0f}s",
        ]
        if self.downloads:
            facts.append("downloads: " + ", ".join(Path(p).name for p in self.downloads))
        if self.injection_detected:
            facts.append("page tried to instruct the agent (ignored)")
        if self.captcha_detected:
            facts.append("site presented a bot check")
        return outcome.untrusted("browser", body) + (
            "\n[browser agent: " + "; ".join(f for f in facts if f) + "]"
        )


# ── interpreter + preflight ─────────────────────────────────────────────────


def resolve_python() -> str:
    """The browser-use interpreter: explicit override, dedicated venv, then us."""
    override = os.environ.get("JARVIS_BROWSER_PYTHON", "").strip()
    if override:
        return override
    if _DEFAULT_VENV.exists():
        return str(_DEFAULT_VENV)
    return sys.executable


def preflight(refresh: bool = False) -> tuple[bool, str, str]:
    """(ok, version, reason) for the browser-use install.

    Cached per (interpreter, runner path): if either moves, the old verdict is
    about a different install and must not be reused.
    """
    python = resolve_python()
    key = f"{python}\0{_RUNNER}"
    with _preflight_lock:
        if not refresh and key in _preflight_cache:
            return _preflight_cache[key]

    runner = _RUNNER
    if not runner.exists():
        result = (False, "", f"runner missing: {runner}")
    else:
        try:
            proc = subprocess.run(
                [python, "-c", "import browser_use as b; print(getattr(b, '__version__', ''))"],
                capture_output=True,
                text=True,
                timeout=60,
                cwd=tempfile.gettempdir(),
                env=_child_env(),
            )
        except FileNotFoundError:
            result = (False, "", f"interpreter not found: {python}")
        except subprocess.TimeoutExpired:
            result = (False, "", "importing browser_use timed out")
        except Exception as exc:
            result = (False, "", f"{type(exc).__name__}: {exc}")
        else:
            version = (proc.stdout or "").strip()
            if proc.returncode == 0 and version:
                result = (True, version, "ready")
            else:
                err = (proc.stderr or "").strip().splitlines()
                result = (
                    False,
                    version,
                    f"browser-use not importable ({err[-1][:180] if err else 'no output'})",
                )

    with _preflight_lock:
        _preflight_cache[key] = result
    return result


def _child_env() -> dict:
    """A deliberately small environment for the browser subprocess.

    Provider keys are read from config/api_keys.json by the runner itself, so
    they never need to be in the environment. HOME/PATH stay so Chrome and the
    profile resolve correctly.
    """
    keep = ("HOME", "PATH", "LANG", "LC_ALL", "TMPDIR", "DISPLAY", "WAYLAND_DISPLAY",
            "XAUTHORITY", "DBUS_SESSION_BUS_ADDRESS", "XDG_RUNTIME_DIR")
    env = {k: os.environ[k] for k in keep if k in os.environ}
    env.setdefault("PATH", "/usr/local/bin:/usr/bin:/bin")
    env["ANONYMIZED_TELEMETRY"] = "false"
    return env


def _task_hash(task: str) -> str:
    norm = " ".join(str(task or "").lower().split())
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()[:16]


# ── jobs ────────────────────────────────────────────────────────────────────


@dataclass
class _Job:
    job_id: str
    task: str
    task_hash: str
    started_at: float
    workspace: Path
    downloads_dir: Path
    done: bool = False
    cancelled: bool = False
    holds_browser: bool = False
    plan: dict = field(default_factory=dict)
    result: Optional[BrowserRunResult] = None
    proc: Optional[subprocess.Popen] = None
    stderr: collections.deque = field(default_factory=lambda: collections.deque(maxlen=120))


class BrowserRuntime:
    """Thread-safe job registry around the runner."""

    def __init__(self, cfg: BrowserAgentConfig | None = None):
        self.cfg = cfg or load_config()
        self._lock = threading.RLock()
        self._jobs: dict[str, _Job] = {}
        self._done: dict[str, BrowserRunResult] = {}
        # One local profile, one Chrome. Two agents sharing it make Chrome exit
        # with "another Chrome is using this profile" — so a browser job takes
        # this lock and a second one is told, honestly, that the browser is busy.
        self._browser_lock = threading.Lock()

    # ── public API ────────────────────────────────────────────────────────
    def start(
        self,
        task: str,
        *,
        mode: str = "auto",
        max_steps: int | None = None,
        visible: bool = False,
        auth: bool = False,
        site: str | None = None,
        domains: str | tuple[str, ...] = (),
        confirmed: bool = False,
        fresh_profile: bool = False,
        channel: str = "",
        log: Optional[Callable[[str], None]] = None,
        wait: bool = False,
    ) -> BrowserRunResult:
        task = str(task or "").strip()
        cfg = self.cfg.with_domains(domains)
        plan = router.plan_task(task, mode=mode, cfg=cfg)

        decision = policy.classify(task)
        if decision.verdict is policy.Verdict.DENY:
            return self._finish_immediate(
                task, plan, BrowserRunResult(
                    job_id="", ok=False, status="denied",
                    error=decision.reason, plan=plan.public(), task=task,
                ), cfg,
            )
        if decision.verdict is policy.Verdict.NEEDS_APPROVAL and not confirmed:
            return self._finish_immediate(
                task, plan, BrowserRunResult(
                    job_id="", ok=False, status="needs_approval",
                    error=decision.reason, plan=plan.public(), task=task,
                ), cfg,
            )

        if plan.kind == "tool":
            return self._finish_immediate(
                task, plan, BrowserRunResult(
                    job_id="", ok=False, status="delegated",
                    error=(
                        f"No browser needed: {plan.reason}. "
                        "Use the matching existing tool instead."
                    ),
                    plan=plan.public(), task=task,
                ), cfg,
            )

        ok, _version, reason = preflight()
        if not ok:
            return self._finish_immediate(
                task, plan, BrowserRunResult(
                    job_id="", ok=False, status="unavailable",
                    error=f"the browser runtime is not installed — {reason}",
                    plan=plan.public(), task=task,
                ), cfg,
            )

        hash_ = _task_hash(task)
        with self._lock:
            for job in self._jobs.values():
                if job.task_hash == hash_ and not job.done:
                    return BrowserRunResult(
                        job_id=job.job_id, ok=False, status="duplicate",
                        error=f"an identical task is already running ({job.job_id})",
                        plan=plan.public(), task=task,
                    )
            job_id = "bjob-" + uuid.uuid4().hex[:10]
            workspace = Path(tempfile.mkdtemp(prefix=f"rio-browser-{job_id}-"))
            job = _Job(
                job_id=job_id,
                task=task,
                task_hash=hash_,
                started_at=time.monotonic(),
                workspace=workspace,
                downloads_dir=cfg.downloads_dir / job_id,
                plan=plan.public(),
            )
            self._jobs[job_id] = job

        if plan.kind == "http":
            thread = threading.Thread(
                target=self._run_http, args=(job, plan, log), daemon=True,
                name=f"browser-http-{job_id}",
            )
        else:
            busy = self._acquire_browser(job)
            if busy is not None:
                with self._lock:
                    job.done = True
                    job.result = busy
                    self._done[job_id] = busy
                self._journal(busy)
                return busy
            thread = threading.Thread(
                target=self._run_browser,
                args=(job, cfg, max_steps, visible, auth, site, fresh_profile, log),
                daemon=True,
                name=f"browser-run-{job_id}",
            )
        thread.start()

        if wait:
            return self.wait(job_id)
        return BrowserRunResult(
            job_id=job_id,
            ok=False,
            status="running",
            error="",
            plan=plan.public(),
            task=task,
        )

    def wait(self, job_id: str, timeout: float | None = None) -> BrowserRunResult:
        deadline = time.monotonic() + (timeout if timeout is not None else self.cfg.timeout_s + 30)
        while time.monotonic() < deadline:
            with self._lock:
                job = self._jobs.get(job_id)
            if job is None:
                return self._done.get(job_id) or BrowserRunResult(
                    job_id=job_id, ok=False, status="unknown", error="no such job"
                )
            if job.done and job.result is not None:
                return job.result
            time.sleep(0.25)
        return BrowserRunResult(
            job_id=job_id, ok=False, status="timeout",
            error="the job exceeded its budget and will be cancelled",
        )

    def status(self, job_id: str) -> BrowserRunResult:
        with self._lock:
            job = self._jobs.get(job_id)
        if job and job.result is not None:
            return job.result
        if job:
            return BrowserRunResult(
                job_id=job_id, ok=False, status="running", task=job.task,
                duration_s=round(time.monotonic() - job.started_at, 1),
            )
        return self._done.get(job_id) or BrowserRunResult(
            job_id=job_id, ok=False, status="unknown", error="no such job"
        )

    def cancel(self, job_id: str) -> BrowserRunResult:
        with self._lock:
            job = self._jobs.get(job_id)
        if job is None:
            return self._done.get(job_id) or BrowserRunResult(
                job_id=job_id, ok=False, status="unknown", error="no such job"
            )
        if job.done:
            return job.result or BrowserRunResult(job_id=job_id, ok=False, status="done")
        job.cancelled = True
        proc = job.proc
        if proc and proc.poll() is None:
            _kill_group(proc)
        # The worker thread finishes the record; give it a moment, then report.
        deadline = time.monotonic() + _TERM_GRACE_S + 2
        while time.monotonic() < deadline and not job.done:
            time.sleep(0.1)
        if job.done and job.result is not None:
            return job.result
        # The worker is still blocked in communicate() (a child ignoring
        # SIGTERM, a wedged pipe). Mark the job finished now so it stops
        # blocking duplicates; the worker's late record overwrites this one.
        cancelled = BrowserRunResult(
            job_id=job_id, ok=False, status="cancelled",
            error="cancelled by the user", task=job.task,
        )
        with self._lock:
            if not job.done:
                job.done = True
                job.result = cancelled
                self._done[job_id] = cancelled
                # A wedged child must not keep the one-browser slot forever.
                if job.holds_browser:
                    job.holds_browser = False
                    try:
                        self._browser_lock.release()
                    except RuntimeError:
                        pass
        return cancelled

    def active(self) -> list[dict]:
        with self._lock:
            return [
                {
                    "job_id": j.job_id,
                    "task": j.task[:160],
                    "elapsed_s": round(time.monotonic() - j.started_at, 1),
                }
                for j in self._jobs.values()
                if not j.done
            ]

    # ── workers ───────────────────────────────────────────────────────────
    def _run_http(self, job: _Job, plan: router.Plan, log) -> None:
        result = BrowserRunResult(
            job_id=job.job_id, ok=False, status="error", plan=plan.public(), task=job.task
        )
        fallback_why = ""
        try:
            page = router.fetch_static(plan.url or "", timeout=self.cfg.http_timeout_s)
            thin = len(page.text.strip()) < self.cfg.http_min_chars
            if page.usable and not thin:
                body = page.text.strip()
                if page.title:
                    body = f"{page.title}\n\n{body}"
                result.ok = True
                result.status = "completed"
                result.final = body
                result.brain = "http"
                result.model = "requests+bs4"
            else:
                fallback_why = page.error or (
                    f"only {len(page.text.strip())} chars of text (JS shell)"
                )
        except Exception as exc:
            result.status = "error"
            result.error = f"{type(exc).__name__}: {exc}"

        if fallback_why:
            if log:
                log(f"[browser] static fetch unusable ({fallback_why}) — using the browser")
            result = self._run_browser_job(
                job, None, False, False, None, log,
                note=f"static fetch unusable: {fallback_why}",
            )
            result.plan = plan.public()

        self._complete(job, result)

    def _run_browser(self, job: _Job, cfg, max_steps, visible, auth, site, fresh_profile, log) -> None:
        try:
            result = self._run_browser_job(
                job, max_steps, visible, auth, site, log, fresh_profile=fresh_profile
            )
        except Exception as exc:
            result = BrowserRunResult(
                job_id=job.job_id, ok=False, status="error",
                error=f"{type(exc).__name__}: {exc}", task=job.task,
            )
        self._complete(job, result)

    def _run_browser_job(
        self, job: _Job, max_steps, visible, auth, site, log,
        note: str = "", fresh_profile: bool = False,
    ) -> BrowserRunResult:
        cfg = self.cfg
        busy = self._acquire_browser(job)
        if busy is not None:
            return busy
        python = resolve_python()
        job.downloads_dir.mkdir(parents=True, exist_ok=True)
        cmd = [
            python,
            str(_RUNNER),
            job.task,
            "--json",
            "--max-steps", str(cfg.bounded_steps(max_steps)),
            "--brain", cfg.brain if cfg.brain in ALLOWED_BRAINS else "auto",
            "--timeout", str(float(cfg.timeout_s)),
            "--workspace", str(job.workspace),
            "--downloads", str(job.downloads_dir),
            "--keep-workspace",
        ]
        if visible:
            cmd.append("--visible")
        if auth or cfg.attach_chrome_default:
            cmd.append("--attach-chrome")
        if fresh_profile:
            cmd.append("--no-persist")
        if site:
            cmd += ["--site", site]
        domains = policy.domain_policy(cfg)
        if domains["allowed_domains"]:
            cmd += ["--allowed-domains", ",".join(domains["allowed_domains"])]
        if domains["prohibited_domains"]:
            cmd += ["--prohibited-domains", ",".join(domains["prohibited_domains"])]
        if not cfg.judge:
            cmd.append("--no-judge")

        t0 = time.monotonic()
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            start_new_session=True,
            env=_child_env(),
            cwd=str(job.workspace),
        )
        with self._lock:
            job.proc = proc

        def _drain() -> None:
            try:
                assert proc.stderr is not None
                for line in proc.stderr:
                    line = line.rstrip()
                    if not line:
                        continue
                    job.stderr.append(line)
                    if log and line.startswith("[browser-use]"):
                        log(line[:200])
            except Exception:
                pass

        drain = threading.Thread(target=_drain, daemon=True, name=f"browser-log-{job.job_id}")
        drain.start()

        timed_out = False
        try:
            stdout, _ = proc.communicate(timeout=cfg.timeout_s + _STARTUP_GRACE_S)
        except subprocess.TimeoutExpired:
            timed_out = True
            _kill_group(proc)
            try:
                stdout, _ = proc.communicate(timeout=_TERM_GRACE_S)
            except Exception:
                stdout = ""
        drain.join(timeout=2)

        payload = _parse_json(stdout or "")
        if payload is None:
            tail = " | ".join(list(job.stderr)[-3:])
            if job.cancelled:
                status = "cancelled"
                error = "cancelled by the user"
            elif timed_out:
                status = "timeout"
                error = f"the browser task exceeded its {cfg.timeout_s:.0f}s budget"
            else:
                status = "error"
                error = tail or f"runner exited {proc.returncode} without a result"
            return BrowserRunResult(
                job_id=job.job_id, ok=False, status=status,
                error=error,
                duration_s=round(time.monotonic() - t0, 1),
                task=job.task,
            )

        result = BrowserRunResult(
            job_id=job.job_id,
            ok=bool(payload.get("ok")),
            status=str(payload.get("status") or "error"),
            final=str(payload.get("final") or ""),
            error=str(payload.get("error") or ""),
            brain=str(payload.get("brain") or ""),
            model=str(payload.get("model") or ""),
            steps=int(payload.get("steps") or 0),
            duration_s=float(payload.get("duration_s") or round(time.monotonic() - t0, 1)),
            downloads=tuple(str(p) for p in payload.get("downloads") or ()),
            injection_detected=bool(payload.get("injection_detected")),
            captcha_detected=bool(payload.get("captcha_detected")),
            skipped_models=tuple(payload.get("skipped_models") or ()),
            task=job.task,
        )
        if note and not result.error:
            result.error = note
        if timed_out:
            result.ok = False
            result.status = "timeout"
            result.error = f"the browser task exceeded its {cfg.timeout_s:.0f}s budget"
        if job.cancelled:
            result.ok = False
            result.status = "cancelled"
            result.error = "cancelled by the user"
        result.plan = result.plan or job.plan or router.plan_task(job.task, cfg=cfg).public()
        return result

    # ── completion bookkeeping ────────────────────────────────────────────
    def _acquire_browser(self, job: _Job) -> Optional[BrowserRunResult]:
        """Take the one-browser slot, or explain who has it."""
        if job.holds_browser:
            return None
        if self._browser_lock.acquire(blocking=False):
            job.holds_browser = True
            return None
        holder = next(
            (j["job_id"] for j in self.active() if j["job_id"] != job.job_id), ""
        )
        return BrowserRunResult(
            job_id=job.job_id,
            ok=False,
            status="busy",
            error=(
                "another browser task is already running"
                + (f" ({holder})" if holder else "")
                + "; the local profile allows one browser at a time"
            ),
            task=job.task,
        )

    def _finish_immediate(
        self, task: str, plan: router.Plan, result: BrowserRunResult,
        cfg: BrowserAgentConfig,
    ) -> BrowserRunResult:
        result.text = result.render(cfg)
        self._journal(result)
        return result

    def _complete(self, job: _Job, result: BrowserRunResult) -> None:
        cfg = self.cfg
        result.task = result.task or job.task
        result.text = result.render(cfg)

        # Keep evidence for failures, delete the throwaway workspace on success.
        workspace = job.workspace
        if workspace.exists():
            if result.ok or not cfg.keep_failure_artifacts:
                shutil.rmtree(workspace, ignore_errors=True)
            else:
                target = cfg.failures_dir / job.job_id
                try:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    if target.exists():
                        shutil.rmtree(target, ignore_errors=True)
                    shutil.move(str(workspace), str(target))
                    result.error = result.error or f"artifacts kept at {target}"
                except Exception:
                    shutil.rmtree(workspace, ignore_errors=True)

        with self._lock:
            job.done = True
            job.result = result
            self._done[job.job_id] = result
            if job.holds_browser:
                job.holds_browser = False
                try:
                    self._browser_lock.release()
                except RuntimeError:
                    pass
        self._journal(result)

    def _journal(self, result: BrowserRunResult) -> None:
        """Append one metadata-only record. Never page text, never secrets."""
        try:
            self.cfg.state_dir.mkdir(parents=True, exist_ok=True)
            record = result.public()
            record.update(
                {
                    "task_hash": _task_hash(result.task),
                    "channel": "",
                    "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
                    "plan": result.plan,
                    "skipped_models": [
                        {"provider": m.get("provider"), "model": m.get("model"),
                         "reason": str(m.get("reason", ""))[:120]}
                        for m in result.skipped_models
                    ],
                }
            )
            with self.cfg.runs_log.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        except Exception:
            pass


def _kill_group(proc: subprocess.Popen) -> None:
    """SIGTERM the whole session, then SIGKILL. Never leaves an orphan Chrome."""
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


def _parse_json(stdout: str) -> Optional[dict]:
    for line in reversed(stdout.strip().splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            data = json.loads(line)
            if isinstance(data, dict):
                return data
        except Exception:
            continue
    return None


_RUNTIME_LOCK = threading.Lock()
_RUNTIME: BrowserRuntime | None = None


def shared_runtime(refresh: bool = False) -> BrowserRuntime:
    """Process-wide runtime; one job registry for every entry point."""
    global _RUNTIME
    with _RUNTIME_LOCK:
        if _RUNTIME is None or refresh:
            _RUNTIME = BrowserRuntime(load_config(refresh=refresh))
        return _RUNTIME


def run_task(task: str, **kwargs) -> BrowserRunResult:
    """Convenience: start one task and wait for its real result."""
    kwargs.setdefault("wait", True)
    return shared_runtime().start(task, **kwargs)
