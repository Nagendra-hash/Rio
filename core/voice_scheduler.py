"""
core/voice_scheduler.py — Voice-Activated Scheduler
Cron-like scheduling for voice macros and commands.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

import croniter

logger = logging.getLogger(__name__)


@dataclass
class ScheduledJob:
    """A scheduled voice job."""
    job_id: str
    name: str
    cron_expression: str  # Standard cron expression
    macro_name: str | None = None  # Voice macro to execute
    command: str | None = None  # Raw voice command to execute
    params: dict[str, Any] = field(default_factory=dict)  # Parameters for command
    enabled: bool = True
    created_at: float = field(default_factory=time.time)
    last_run: float | None = None
    next_run: float | None = None
    run_count: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)

    def compute_next_run(self, from_time: float | None = None) -> float:
        """Compute next run time from cron expression."""
        base = datetime.fromtimestamp(from_time or time.time())
        try:
            cron = croniter.croniter(self.cron_expression, base)
            next_dt = cron.get_next(datetime)
            return next_dt.timestamp()
        except Exception as e:
            logger.error(f"Invalid cron expression '{self.cron_expression}': {e}")
            return 0.0

    def to_dict(self) -> dict:
        """Serialize to dict."""
        return {
            "job_id": self.job_id,
            "name": self.name,
            "cron_expression": self.cron_expression,
            "macro_name": self.macro_name,
            "command": self.command,
            "params": self.params,
            "enabled": self.enabled,
            "created_at": self.created_at,
            "last_run": self.last_run,
            "next_run": self.next_run,
            "run_count": self.run_count,
            "metadata": self.metadata
        }

    @classmethod
    def from_dict(cls, data: dict) -> "ScheduledJob":
        """Deserialize from dict."""
        return cls(**data)


class VoiceScheduler:
    """Manages scheduled voice commands and macros."""

    def __init__(
        self,
        storage_dir: str = "memory/voice_scheduler",
        executor: Callable[[str, dict], Any] | None = None
    ):
        self.storage_dir = Path(storage_dir)
        self.storage_dir.mkdir(parents=True, exist_ok=True)
        self.jobs: dict[str, ScheduledJob] = {}
        self.executor = executor
        self._running = False
        self._task: asyncio.Task | None = None
        self._load_jobs()
        self._compute_next_runs()

    def _load_jobs(self) -> None:
        """Load jobs from disk."""
        for file in self.storage_dir.glob("*.json"):
            try:
                data = json.loads(file.read_text())
                job = ScheduledJob.from_dict(data)
                self.jobs[job.job_id] = job
            except Exception as e:
                logger.warning(f"Failed to load job from {file}: {e}")

    def _save_job(self, job: ScheduledJob) -> None:
        """Save job to disk."""
        try:
            file = self.storage_dir / f"{job.job_id}.json"
            file.write_text(json.dumps(job.to_dict(), indent=2))
        except Exception as e:
            logger.error(f"Failed to save job {job.job_id}: {e}")

    def _compute_next_runs(self) -> None:
        """Compute next run times for all enabled jobs."""
        now = time.time()
        for job in self.jobs.values():
            if job.enabled:
                job.next_run = job.compute_next_run(now)
            else:
                job.next_run = None

    def add_job(
        self,
        name: str,
        cron_expression: str,
        macro_name: str | None = None,
        command: str | None = None,
        params: dict | None = None,
        job_id: str | None = None
    ) -> ScheduledJob:
        """Add a new scheduled job."""
        if not macro_name and not command:
            raise ValueError("Either macro_name or command must be provided")

        if job_id is None:
            job_id = f"job_{int(time.time() * 1000)}"

        # Validate cron expression
        try:
            croniter.croniter(cron_expression, datetime.now())
        except Exception as e:
            raise ValueError(f"Invalid cron expression: {e}")

        job = ScheduledJob(
            job_id=job_id,
            name=name,
            cron_expression=cron_expression,
            macro_name=macro_name,
            command=command,
            params=params or {}
        )
        job.next_run = job.compute_next_run()

        self.jobs[job_id] = job
        self._save_job(job)
        logger.info(f"Added scheduled job: {name} ({job_id}) - {cron_expression}")

        return job

    def remove_job(self, job_id: str) -> bool:
        """Remove a scheduled job."""
        if job_id in self.jobs:
            del self.jobs[job_id]
            file = self.storage_dir / f"{job_id}.json"
            if file.exists():
                file.unlink()
            logger.info(f"Removed scheduled job: {job_id}")
            return True
        return False

    def enable_job(self, job_id: str, enabled: bool = True) -> bool:
        """Enable or disable a job."""
        if job_id in self.jobs:
            self.jobs[job_id].enabled = enabled
            if enabled:
                self.jobs[job_id].next_run = self.jobs[job_id].compute_next_run()
            else:
                self.jobs[job_id].next_run = None
            self._save_job(self.jobs[job_id])
            return True
        return False

    def get_job(self, job_id: str) -> ScheduledJob | None:
        """Get a job by ID."""
        return self.jobs.get(job_id)

    def list_jobs(self, enabled_only: bool = False) -> list[ScheduledJob]:
        """List all jobs."""
        jobs = list(self.jobs.values())
        if enabled_only:
            jobs = [j for j in jobs if j.enabled]
        return sorted(jobs, key=lambda j: j.next_run or float('inf'))

    def get_due_jobs(self, now: float | None = None) -> list[ScheduledJob]:
        """Get jobs that are due to run."""
        now = now or time.time()
        due = []
        for job in self.jobs.values():
            if job.enabled and job.next_run and job.next_run <= now:
                due.append(job)
        return due

    async def _run_job(self, job: ScheduledJob) -> None:
        """Execute a scheduled job."""
        if not self.executor:
            logger.warning(f"No executor set for job {job.job_id}")
            return

        try:
            if job.macro_name:
                # Execute macro
                result = await self.executor("macro", {
                    "macro": job.macro_name,
                    **job.params
                })
            elif job.command:
                # Execute raw command
                result = await self.executor("command", {
                    "transcript": job.command,
                    **job.params
                })
            else:
                return

            job.last_run = time.time()
            job.run_count += 1
            job.next_run = job.compute_next_run(job.last_run)
            self._save_job(job)

            logger.info(f"Executed scheduled job {job.job_id}: {result}")

        except Exception as e:
            logger.error(f"Job {job.job_id} failed: {e}")

    async def _scheduler_loop(self) -> None:
        """Main scheduler loop."""
        while self._running:
            try:
                now = time.time()
                due_jobs = self.get_due_jobs(now)

                for job in due_jobs:
                    await self._run_job(job)

                # Sleep until next job or 1 minute
                next_runs = [j.next_run for j in self.jobs.values()
                            if j.enabled and j.next_run]
                if next_runs:
                    sleep_time = min(min(next_runs) - now, 60)
                    sleep_time = max(1, sleep_time)
                else:
                    sleep_time = 60

                await asyncio.sleep(sleep_time)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Scheduler loop error: {e}")
                await asyncio.sleep(5)

    def start(self) -> None:
        """Start the scheduler."""
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._scheduler_loop())
        logger.info("Voice scheduler started")

    def stop(self) -> None:
        """Stop the scheduler."""
        self._running = False
        if self._task:
            self._task.cancel()
            self._task = None
        logger.info("Voice scheduler stopped")

    def trigger_job_now(self, job_id: str) -> bool:
        """Manually trigger a job immediately."""
        if job_id in self.jobs and self.executor:
            job = self.jobs[job_id]
            asyncio.create_task(self._run_job(job))
            return True
        return False


# Singleton instance
_scheduler: VoiceScheduler | None = None


def get_voice_scheduler(executor: Callable | None = None) -> VoiceScheduler:
    """Get or create the voice scheduler singleton."""
    global _scheduler
    if _scheduler is None:
        _scheduler = VoiceScheduler(executor=executor)
    elif executor and _scheduler.executor is None:
        _scheduler.executor = executor
    return _scheduler