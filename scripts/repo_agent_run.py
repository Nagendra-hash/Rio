#!/usr/bin/env python3
"""
scripts/repo_agent_run.py — detached worker for the Universal Repository Agent.

Pattern identical to scripts/backtest_run.py --worker: the conversation loop,
dashboard and Hermes NEVER run repository code in-process. This process is
started detached (start_new_session=True) by core/repo_agent/jobs.spawn_worker,
reports progress into the SQLite job store, writes its own redacted logs, and
enforces the job deadline.

    python scripts/repo_agent_run.py --worker rt-20261024-abcdef1234

Kinds handled: install | update | remove | run_skill
"""
from __future__ import annotations

import argparse
import json
import os
import resource
import shutil
import signal
import sys
import time
from pathlib import Path
from typing import Any

BASE = Path(__file__).resolve().parent.parent
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

from core.repo_agent import manifests as M                     # noqa: E402
from core.repo_agent import notify, skills                     # noqa: E402
from core.repo_agent.jobs import (RepoJobStore, config,        # noqa: E402
                                  results_dir, state_for_result)
from core.repo_agent.manager import RepoCancelled, RepositoryManager  # noqa: E402
from core.repo_agent.security import RepoSecurityError, safe_dir_name  # noqa: E402


class _Worker:
    def __init__(self, job_id: str) -> None:
        self.job_id = job_id
        self.store = RepoJobStore()
        self._stop = False
        signal.signal(signal.SIGTERM, self._on_signal)
        signal.signal(signal.SIGINT, self._on_signal)

    def _on_signal(self, *_args) -> None:
        self._stop = True

    # callbacks handed into the manager / runner
    def _cancel(self) -> bool:
        return self._stop or self.store.is_cancelled(self.job_id)

    def _progress(self, phase: str, pct: float, message: str = "") -> None:
        self.store.progress(self.job_id, phase, pct, message)

    # ── main ─────────────────────────────────────────────────────────────
    def run(self) -> int:
        job = self.store.get(self.job_id)
        if not job:
            print(f"unknown job {self.job_id}", file=sys.stderr)
            return 2
        if str(job["status"]) not in ("QUEUED",):
            print(f"job {self.job_id} is {job['status']}, refusing to run",
                  file=sys.stderr)
            return 2
        self.store.mark_running(self.job_id, pid=os.getpid())
        deadline = time.time() + float(job.get("timeout_s") or 3600)
        params = _loads(job.get("params"))
        try:
            if job["kind"] == "install":
                result = self._install(str(params.get("url") or ""), params)
            elif job["kind"] == "update":
                result = self._update(str(job.get("ref") or ""))
            elif job["kind"] == "remove":
                result = self._remove(str(job.get("ref") or ""))
            elif job["kind"] == "run_skill":
                result = self._run_skill(job, params)
            elif job["kind"] == "video":
                result = self._run_video(job, params)
            elif job["kind"] == "engine_install":
                result = self._run_engine_install(params)
            else:
                result = {"status": "FAILED",
                          "error": f"unknown job kind {job['kind']}"}
        except RepoCancelled:
            result = {"status": "CANCELLED", "message": "cancelled by user"}
        except TimeoutError:
            result = {"status": "FAILED", "error": "job exceeded its deadline"}
        except RepoSecurityError as exc:
            result = {"status": "FAILED", "error": str(exc)}
        except Exception as exc:                      # noqa: BLE001
            result = {"status": "FAILED",
                      "error": f"{type(exc).__name__}: {str(exc)[:400]}"}

        if time.time() > deadline and result.get("status") == "RUNNING":
            result = {"status": "FAILED", "error": "job exceeded its deadline"}
        usage = resource.getrusage(resource.RUSAGE_CHILDREN)
        result.setdefault("ram_peak_mb",
                          round(usage.ru_maxrss / 1024.0, 1))
        # write the result artifact BEFORE the job row turns terminal —
        # a COMPLETED row must never outlive its own result file. The
        # placeholder already carries the terminal state that finish() will
        # apply, so a reader that sees COMPLETED in the DB never opens this
        # file and finds PENDING in it (the in-suite flake, 2026-10-24).
        # Only the rare user-cancelled race is corrected by the rewrite below.
        M.write_json(results_dir() / f"{self.job_id}.json",
                     {"job_id": self.job_id, "kind": job.get("kind"),
                      "ref": job.get("ref"),
                      "state": state_for_result(result),
                      "result": result})
        state = self.store.finish(self.job_id, result)
        job = self.store.get(self.job_id) or job
        try:
            M.write_json(results_dir() / f"{self.job_id}.json",
                         {"job_id": self.job_id, "kind": job.get("kind"),
                          "ref": job.get("ref"), "state": state,
                          "result": result})
        except Exception:
            pass
        try:
            notify.notify_job_result(job, result)
        except Exception:
            pass
        self.store.dispatch_queued()
        return 0 if state in ("COMPLETED", "NEEDS_CONFIGURATION",
                              "WAITING_FOR_APPROVAL") else 1

    # ── kind handlers ────────────────────────────────────────────────────
    def _install(self, url: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        params = params or {}
        cfg = config()
        manager = RepositoryManager(progress=self._progress,
                                    cancel=self._cancel)
        result = manager.install(
            url, min_free_disk_gb=float(cfg.get("min_free_disk_gb", 10)),
            allow_local=bool(params.get("allow_local")))
        if result.get("ok"):
            self._refresh_skill_state(str(result.get("name") or ""))
        return result

    def _update(self, name: str) -> dict[str, Any]:
        manager = RepositoryManager(progress=self._progress,
                                    cancel=self._cancel)
        result = manager.update(name)
        if result.get("ok"):
            self._refresh_skill_state(safe_dir_name(name))
        return result

    def _remove(self, name: str) -> dict[str, Any]:
        manager = RepositoryManager(progress=self._progress,
                                    cancel=self._cancel)
        return manager.remove(name)

    # ── video pipeline jobs (core/video_agent) ───────────────────────────
    def _run_video(self, job: dict, params: dict) -> dict[str, Any]:
        from core.video_agent.runner import execute_video_job
        return execute_video_job(
            job, params,
            progress=self._progress, cancelled=self._cancel,
            log=lambda m: print(m, flush=True),
            deadline=time.time() + float(job.get("timeout_s") or 3600))

    def _run_engine_install(self, params: dict) -> dict[str, Any]:
        """Phase 4: approved, detached, cancellable engine installation."""
        from core.video_agent.installer import execute_install
        return execute_install(
            str((params or {}).get("engine_id") or ""),
            params=params or {},
            progress=self._progress, cancel=self._cancel)

    def _run_skill(self, job: dict, params: dict[str, Any]) -> dict[str, Any]:
        skill_id = str(job.get("ref") or "")
        view = skills.resolve_skill(skill_id)
        entry = view.get("entry") or {}
        if not view.get("ok"):
            return {"status": "FAILED", "error": f"unknown skill {skill_id}"}
        if not entry.get("enabled", True):
            return {"status": "FAILED",
                    "error": f"skill {skill_id} is disabled"}
        if str(entry.get("status") or "READY") != "READY":
            return {"status": "NEEDS_CONFIGURATION",
                    "message": f"{skill_id} is {entry.get('status')} — "
                               "complete its provider configuration first "
                               "(config/api_keys.json)"}
        out_dir = str(params.get("out_dir") or "").strip()
        if out_dir:
            out_dir = str(Path(out_dir).expanduser().resolve())
            Path(out_dir).mkdir(parents=True, exist_ok=True)
        result = skills.run_skill(
            skill_id, str(params.get("op") or ""),
            dict(params.get("args") or {}),
            progress=self._progress, cancel=self._cancel,
            run_context={"out_dir": out_dir,
                         "log_base": M.logs_dir() / self.job_id})
        self._sweep_scratch(result.get("out_dir"))
        self._verify_artifacts(skill_id, result)
        return result

    # ── helpers ──────────────────────────────────────────────────────────
    @staticmethod
    def _refresh_skill_state(name: str) -> None:
        """After install/update: mark the skill READY or NEEDS_CONFIGURATION
        based on its provider configuration — honestly, never aspirationally."""
        try:
            from core.repo_agent.vimax import provider_status
        except Exception:
            return
        try:
            from core.repo_agent import manifests as M
            skill = M.load_skill_manifest(name)
            if not skill:
                return
            if str(skill.get("skill_id")) == "vimax-video":
                status = provider_status()
                M.set_skill_state(
                    "vimax-video",
                    status="READY" if status["ready"] else
                    "NEEDS_CONFIGURATION",
                    detail=("providers ready" if status["ready"] else
                            "missing: " + ", ".join(status["missing"])))
        except Exception:
            pass

    @staticmethod
    def _sweep_scratch(out_dir: str | None) -> None:
        """Remove leftover scratch files that may hold provider keys."""
        if not out_dir:
            return
        try:
            root = Path(out_dir)
            for name in ("vimax_run.yaml", "params.json"):
                for path in root.rglob(name):
                    try:
                        path.unlink()
                    except OSError:
                        pass
        except Exception:
            pass

    @staticmethod
    def _verify_artifacts(skill_id: str, result: dict[str, Any]) -> None:
        """A video job is COMPLETED only when the file really verifies."""
        if skill_id != "vimax-video" or result.get("status") != "COMPLETED":
            return
        try:
            from core.repo_agent.vimax import find_latest_video, verify_video
        except Exception:
            return
        best = find_latest_video(result.get("out_dir") or "")
        if not best:
            result["status"] = "FAILED"
            result["error"] = ("no playable video artifact was produced — "
                               "not reporting success")
            return
        verification = verify_video(best)
        result["verification"] = verification
        if not verification.get("ok"):
            result["status"] = "FAILED"
            result["error"] = (f"final artifact failed verification: "
                               f"{verification.get('error')}")
            return
        result["outputs"] = [best]
        result["message"] = (f"video ready: {best} "
                             f"({verification.get('duration_s', '?')}s)")


def _loads(raw: Any) -> dict[str, Any]:
    try:
        value = json.loads(raw) if isinstance(raw, str) else (raw or {})
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker", help="job id to execute")
    args = parser.parse_args()
    if not args.worker:
        parser.error("--worker <job_id> is required")
    return _Worker(args.worker).run()


if __name__ == "__main__":
    sys.exit(main())
