"""
core/video_agent/resources.py — resource management & safety (Phase 10).

Everything here is measurable and offline:

  * ffmpeg_threads()      thread cap from the REAL cpu count (2C/4T laptop)
  * free_disk_gb()        real free space under a path
  * ensure_disk()         refuse to start a render without headroom
  * RenderLock            ONE heavy render at a time across worker processes
                          (lock file + live-pid liveness; stale locks break)
  * atomic_final()        final MP4s appear only when complete (encode to a
                          .part temp, os.replace on success) — no partial
                          outputs can ever be mistaken for results
  * cleanup_run_dir()     bounded cache: intermediates (work/, .part files)
                          are deleted after a run; source assets (narration),
                          script/research and the final MP4 are preserved.
                          User-provided originals never live in the run dir
                          (they are copied), so cleanup can never touch them.
  * attempt caps          the retry action refuses beyond video_max_attempts

Config keys (repo_agent section / JARVIS_REPO_AGENT_* env): see
core/repo_agent/jobs.py DEFAULTS — video_min_free_gb, video_render_lock_wait_s,
video_max_attempts.
"""
from __future__ import annotations

import os
import shutil
import time
from pathlib import Path
from typing import Any

from core.repo_agent.jobs import config as jobs_config
from core.video_agent.pipelines import StageError

LOCK_NAME = "render.lock"


def ffmpeg_threads() -> int:
    """Resource-aware ffmpeg thread cap: never all cores, never 0."""
    override = str(jobs_config().get("video_ffmpeg_threads") or "").strip()
    if override.isdigit() and int(override) > 0:
        return int(override)
    cpus = os.cpu_count() or 2
    return max(1, min(2, cpus - 1))


def free_disk_gb(path: Path) -> float:
    try:
        return shutil.disk_usage(str(path)).free / 1024 ** 3
    except Exception:                              # noqa: BLE001
        return -1.0


def ensure_disk(path: Path, needed_gb: float | None = None) -> float:
    """Refuse to start a render without headroom (mission: check free disk
    BEFORE starting). Returns the measured free GB; raises StageError."""
    cfg = jobs_config()
    minimum = float(needed_gb if needed_gb is not None
                    else cfg.get("video_min_free_gb") or 2)
    free = free_disk_gb(path)
    if free >= 0 and free < minimum:
        raise StageError(
            f"not enough free disk space to render: {free:.1f} GB free "
            f"under {path}, need {minimum:.1f} GB — free space and retry "
            "(nothing was started)")
    return free


class RenderLock:
    """File lock serializing heavy renders across the detached workers.

    acquire() waits up to `timeout_s` (config video_render_lock_wait_s,
    default 300) while the holder is ALIVE; a stale lock (dead pid, e.g.
    after a crash) is broken immediately. Use as a context manager."""

    def __init__(self, run_dir: Path, timeout_s: float | None = None,
                 log=None) -> None:
        self.path = Path(run_dir) / LOCK_NAME
        cfg = jobs_config()
        self.timeout_s = float(timeout_s if timeout_s is not None
                               else cfg.get("video_render_lock_wait_s")
                               or 300)
        self.log = log or (lambda _m: None)
        self._acquired = False

    @staticmethod
    def _pid_alive(pid: int) -> bool:
        if pid <= 0:
            return False
        try:
            os.kill(pid, 0)                     # signal 0 = liveness probe
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True                         # exists, not ours — alive
        except OSError:
            return False

    def _try_create(self) -> bool:
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            with os.fdopen(fd, "w") as fh:
                fh.write(str(os.getpid()))
            return True
        except FileExistsError:
            return False

    def _break_stale(self) -> bool:
        """True when an existing lock was held by a DEAD process and was
        removed (crash recovery — never waits forever on a ghost)."""
        try:
            pid = int(self.path.read_text().strip() or "0")
        except Exception:                          # noqa: BLE001
            self.path.unlink(missing_ok=True)
            return True
        if pid <= 0 or not self._pid_alive(pid):
            self.path.unlink(missing_ok=True)
            self.log(f"[resources] broke stale render lock (pid {pid} gone)")
            return True
        return False

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.time() + self.timeout_s
        while True:
            if self._try_create():
                self._acquired = True
                return
            if self._break_stale():
                continue
            if time.time() >= deadline:
                raise StageError(
                    "another video render is still running — waited "
                    f"{self.timeout_s:.0f}s for the render lock. Heavy "
                    "pipelines run one at a time on this machine; cancel "
                    "the other job or wait, then retry.")
            self.log("[resources] waiting for the render lock…")
            time.sleep(2.0)

    def release(self) -> None:
        if self._acquired:
            self.path.unlink(missing_ok=True)
            self._acquired = False

    def __enter__(self) -> "RenderLock":
        self.acquire()
        return self

    def __exit__(self, *exc) -> None:
        self.release()


def atomic_final(tmp_path: Path, final_path: Path) -> Path:
    """Encode targets: write to tmp_path (same filesystem), then os.replace —
    a crashed/interrupted encode can never leave a partial file at the FINAL
    path that a later library scan might mistake for a finished video."""
    final_path = Path(final_path)
    final_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = Path(tmp_path)
    if tmp_path.exists():
        os.replace(tmp_path, final_path)
    return final_path


def cleanup_run_dir(run_dir: Path, keep_dirs: tuple[str, ...] = ("render",
                         "research", "script", "narration", "cards",
                         "assets")) -> dict[str, Any]:
    """Bounded cache after a run: delete the work/ intermediates (per-scene
    encodes, joined.mp4, .part files) and any leftover *.part. Source assets
    (narration/cards), script/research and the final MP4 are preserved.
    User-provided ORIGINALS never live in the run dir, so cleanup cannot
    touch them. Returns what was freed."""
    run_dir = Path(run_dir)
    freed = 0
    removed: list[str] = []
    work = run_dir / "work"
    if work.is_dir():
        for p in sorted(work.rglob("*"), reverse=True):
            try:
                if p.is_file():
                    freed += p.stat().st_size
                    p.unlink()
                elif p.is_dir() and not any(p.iterdir()):
                    p.rmdir()
            except OSError:
                continue
        try:
            work.rmdir()
            removed.append("work/")
        except OSError:
            pass
    for part in run_dir.rglob("*.part"):
        try:
            freed += part.stat().st_size
            part.unlink()
            removed.append(part.name)
        except OSError:
            continue
    return {"freed_mb": round(freed / 1024 / 1024, 1), "removed": removed}


def max_video_attempts() -> int:
    try:
        return max(1, int(jobs_config().get("video_max_attempts") or 3))
    except (TypeError, ValueError):
        return 3
