"""
core/repo_agent/jobs.py — durable job state for the Universal Repository Agent.

States: QUEUED → INSTALLING | RUNNING → COMPLETED | FAILED | CANCELLED
        (+ NEEDS_CONFIGURATION / WAITING_FOR_APPROVAL as terminal-hold states
         produced by workers; a NEEDS_CONFIGURATION install job can be
         re-run after the user adds provider keys)

Modeled on the proven orchestrator/agents/backtest/jobs.py JobStore:

    * SQLite (stdlib, WAL) at memory/repo_agent/jobs.db
    * unique job ids  rt-YYYYMMDD-hex10
    * dedup: an active job with the same (kind, ref, params) is returned
      instead of starting a duplicate — repeated voice events can never
      launch the same install/video twice; a very recent identical
      COMPLETED job is reused within a short window
    * clean cancellation: flag + SIGTERM to the worker's process group,
      short grace, then SIGKILL
    * restart recovery: RUNNING/INSTALLING rows whose pid is gone are
      marked FAILED with an explicit reason (never silently re-run)
    * per-job stdout/stderr logs with secret redaction applied by the worker
    * bounded concurrency: at most max_concurrent detached workers
      (dispatch_queued promotes QUEUED jobs as slots free up)
"""
from __future__ import annotations

import json
import os
import signal
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

STATES = ("QUEUED", "INSTALLING", "RUNNING", "READY",
          "WAITING_FOR_APPROVAL", "NEEDS_CONFIGURATION",
          "COMPLETED", "FAILED", "CANCELLED")
ACTIVE = ("QUEUED", "INSTALLING", "RUNNING")
HOLD = ("WAITING_FOR_APPROVAL", "NEEDS_CONFIGURATION")
TERMINAL = ("COMPLETED", "FAILED", "CANCELLED")
TRANSIENT_HINTS = ("timed out", "timeout", "connection", "429", "rate limit",
                   "temporarily", "network", "HTTP 5", "reset by peer",
                   "resolve", "temporary failure")

ROOT = Path(__file__).resolve().parents[2]
WORKER = ROOT / "scripts" / "repo_agent_run.py"

DEFAULTS = {
    "max_concurrent": 2,          # detached worker processes at once
    "dedup_window_s": 300,        # reuse a fresh identical completion
    "install_timeout_s": 1800,    # 30 min per install/update
    "run_timeout_s": 3600,        # default per skill run (manifest may override)
    "min_free_disk_gb": 10,       # refuse installs below this
    "notify_hermes": True,
    "notify_target": "telegram",  # bare platform = home channel (Hermes CLI
                                  # requires --to; measured 2026-10-25)
    "notify_attach_video_mb": 45, # Hermes attachment cap for finished videos
    # Phase 10: video resource guards
    "video_min_free_gb": 2,       # refuse renders below this headroom
    "video_render_lock_wait_s": 300,  # wait for the single heavy render slot
    "video_max_attempts": 3,      # retry-action cap per job
    "video_ffmpeg_threads": "",   # "" = resource-aware; number = override
    # VAAPI encoder (Phase 2 of the hardware-encoder upgrade)
    "video_encoder": "auto",      # auto | vaapi | libx264
    "video_vaapi_device": "",     # "" = stable PCI by-path render node
    "video_vaapi_driver": "i965",
    "video_vaapi_bitrate": "5M",
    "video_vaapi_maxrate": "7M",
    "video_vaapi_bufsize": "10M",
    "video_x264_crf": 26,
    "video_min_free_mb": 700,     # RAM floor before a render starts
}


def config() -> dict[str, Any]:
    """Job-engine configuration. Overridable via memory config keys of the
    form repo_agent.<key> (read lazily so tests can point JARVIS_HOME
    elsewhere); environment JARVIS_REPO_AGENT_* wins for ops use."""
    import os
    import json
    from pathlib import Path
    from memory.config_manager import BASE_DIR, CONFIG_FILE

    cfg = dict(DEFAULTS)

    # 1) Try new Pydantic Settings first (for env vars and .env)
    try:
        from memory.config_manager import load_api_keys
        section = (load_api_keys() or {}).get("repo_agent") or {}
        if isinstance(section, dict):
            for key in DEFAULTS:
                if key in section:
                    cfg[key] = section[key]
    except Exception:
        pass

    # 2) Also read from old api_keys.json for repo_agent section (backward compat)
    try:
        if CONFIG_FILE.exists():
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            section = data.get("repo_agent") or {}
            if isinstance(section, dict):
                for key in DEFAULTS:
                    if key in section:
                        cfg[key] = section[key]
    except Exception:
        pass
    env_map = {
        "max_concurrent": "JARVIS_REPO_AGENT_MAX_CONCURRENT",
        "install_timeout_s": "JARVIS_REPO_AGENT_INSTALL_TIMEOUT_S",
        "run_timeout_s": "JARVIS_REPO_AGENT_RUN_TIMEOUT_S",
        "min_free_disk_gb": "JARVIS_REPO_AGENT_MIN_FREE_DISK_GB",
        "notify_hermes": "JARVIS_REPO_AGENT_NOTIFY_HERMES",
        "notify_target": "JARVIS_REPO_AGENT_NOTIFY_TARGET",
        "notify_attach_video_mb": "JARVIS_REPO_AGENT_NOTIFY_ATTACH_VIDEO_MB",
        "video_min_free_gb": "JARVIS_REPO_AGENT_VIDEO_MIN_FREE_GB",
        "video_render_lock_wait_s": "JARVIS_REPO_AGENT_VIDEO_LOCK_WAIT_S",
        "video_max_attempts": "JARVIS_REPO_AGENT_VIDEO_MAX_ATTEMPTS",
        "video_ffmpeg_threads": "JARVIS_REPO_AGENT_VIDEO_FFMPEG_THREADS",
        "video_encoder": "JARVIS_VIDEO_ENCODER",
        "video_vaapi_device": "JARVIS_VIDEO_VAAPI_DEVICE",
        "video_vaapi_driver": "JARVIS_VIDEO_VAAPI_DRIVER",
        "video_vaapi_bitrate": "JARVIS_VIDEO_VAAPI_BITRATE",
        "video_vaapi_maxrate": "JARVIS_VIDEO_VAAPI_MAXRATE",
        "video_vaapi_bufsize": "JARVIS_VIDEO_VAAPI_BUFSIZE",
        "video_x264_crf": "JARVIS_VIDEO_X264_CRF",
        "video_min_free_mb": "JARVIS_VIDEO_MIN_FREE_MB",
    }
    import os as _os
    for key, var in env_map.items():
        raw = _os.environ.get(var, "").strip()
        if raw:
            try:
                cfg[key] = type(DEFAULTS[key])(raw)
            except Exception:
                pass
    return cfg


def db_path() -> Path:
    import os
    home = os.environ.get("JARVIS_HOME", "").strip()
    base = Path(home).expanduser() if home else ROOT
    return base / "memory" / "repo_agent" / "jobs.db"


def logs_dir() -> Path:
    return db_path().parent / "job_logs"


def results_dir() -> Path:
    return db_path().parent / "results"


def new_job_id() -> str:
    return f"rt-{datetime.now(timezone.utc).strftime('%Y%m%d')}-{uuid.uuid4().hex[:10]}"


class RepoJobStore:
    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path else db_path()
        self._lock = threading.RLock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        logs_dir().mkdir(parents=True, exist_ok=True)
        results_dir().mkdir(parents=True, exist_ok=True)
        self._init()

    # ── plumbing ─────────────────────────────────────────────────────────
    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=20)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=15000")
        return conn

    def _init(self) -> None:
        with self._lock, self._conn() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS jobs (
                    job_id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    ref TEXT DEFAULT '',
                    source TEXT DEFAULT '',
                    status TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    started_at REAL,
                    finished_at REAL,
                    pid INTEGER,
                    attempts INTEGER DEFAULT 0,
                    progress REAL DEFAULT 0,
                    phase TEXT DEFAULT '',
                    message TEXT DEFAULT '',
                    error TEXT DEFAULT '',
                    log_path TEXT DEFAULT '',
                    outputs TEXT DEFAULT '[]',
                    runtime_s REAL,
                    ram_peak_mb REAL,
                    timeout_s INTEGER,
                    dedup_key TEXT DEFAULT '',
                    cancel_requested INTEGER DEFAULT 0,
                    params TEXT DEFAULT '{}'
                )""")
            conn.execute("""CREATE INDEX IF NOT EXISTS idx_jobs_status
                            ON jobs(status, created_at)""")
            conn.execute("""CREATE INDEX IF NOT EXISTS idx_jobs_dedup
                            ON jobs(dedup_key, status)""")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS notifications (
                    dedup_key TEXT PRIMARY KEY,
                    created_at REAL NOT NULL,
                    event TEXT DEFAULT '',
                    job_id TEXT DEFAULT '',
                    channel TEXT DEFAULT 'hermes',
                    delivered INTEGER DEFAULT 0,
                    detail TEXT DEFAULT ''
                )""")
            conn.execute("""CREATE TABLE IF NOT EXISTS kv (
                            k TEXT PRIMARY KEY, v TEXT, updated_at REAL)""")

    # ── create / dedup ───────────────────────────────────────────────────
    def create(self, kind: str, ref: str, *, source: str = "cli",
               params: dict | None = None,
               dedup_window_s: float | None = None) -> dict[str, Any]:
        kind = str(kind or "").strip().lower()
        ref = str(ref or "").strip().lower()
        sig = _sig(params or {})
        dedup_key = f"{kind}:{ref}:{sig}"
        cfg = config()
        window = float(dedup_window_s if dedup_window_s is not None
                       else cfg["dedup_window_s"])
        now = time.time()
        with self._lock, self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM jobs WHERE dedup_key=? AND status IN "
                "('QUEUED','INSTALLING','RUNNING') ORDER BY created_at DESC "
                "LIMIT 1", (dedup_key,)).fetchone()
            if row:
                job = dict(row)
                return {"ok": True, "deduplicated": True, "job": job,
                        "job_id": job["job_id"]}
            recent = conn.execute(
                "SELECT * FROM jobs WHERE dedup_key=? AND status='COMPLETED' "
                "AND created_at > ? ORDER BY created_at DESC LIMIT 1",
                (dedup_key, now - window)).fetchone()
            if recent:
                job = dict(recent)
                return {"ok": True, "deduplicated": True, "recent": True,
                        "job": job, "job_id": job["job_id"]}
            job_id = new_job_id()
            timeout = (cfg["install_timeout_s"] if kind in ("install", "update")
                       else cfg["run_timeout_s"])
            conn.execute(
                "INSERT INTO jobs (job_id, kind, ref, source, status,"
                " created_at, dedup_key, params, phase, message, timeout_s)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (job_id, kind, ref, source, "QUEUED", now, dedup_key,
                 json.dumps(params or {}), "queued",
                 "waiting for a worker slot", int(timeout)))
            job = dict(conn.execute("SELECT * FROM jobs WHERE job_id=?",
                                    (job_id,)).fetchone())
        # NOTE: no spawn here — start_job() spawns exactly one worker after
        # create() returns; dispatch_queued() only promotes QUEUED jobs when
        # slots free up (called from status polls and worker completion).
        return {"ok": True, "deduplicated": False, "job": job,
                "job_id": job_id}

    # ── reads ────────────────────────────────────────────────────────────
    def get(self, job_id: str) -> dict | None:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM jobs WHERE job_id=?",
                               (job_id,)).fetchone()
        return dict(row) if row else None

    def list(self, limit: int = 20, status: str | None = None,
             kind: str | None = None, ref: str | None = None) -> list[dict]:
        query, args = "SELECT * FROM jobs WHERE 1=1", []
        if status:
            query += " AND status=?"
            args.append(status.upper())
        if kind:
            query += " AND kind=?"
            args.append(kind.lower())
        if ref:
            query += " AND ref=?"
            args.append(ref.lower())
        query += " ORDER BY created_at DESC LIMIT ?"
        args.append(int(limit))
        with self._conn() as conn:
            return [dict(r) for r in conn.execute(query, args).fetchall()]

    def count_active(self) -> int:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM jobs WHERE status IN "
                "('QUEUED','INSTALLING','RUNNING')").fetchone()
        return int(row["n"]) if row else 0

    # ── updates ──────────────────────────────────────────────────────────
    def update(self, job_id: str, **fields: Any) -> None:
        if not fields:
            return
        allowed = {"status", "started_at", "finished_at", "pid", "attempts",
                   "progress", "phase", "message", "error", "log_path",
                   "outputs", "runtime_s", "ram_peak_mb", "timeout_s",
                   "cancel_requested", "params"}
        data = {k: v for k, v in fields.items() if k in allowed}
        if not data:
            return
        for key in ("outputs", "params"):
            if key in data and not isinstance(data[key], str):
                data[key] = json.dumps(data[key], default=str)
        sets = ", ".join(f"{k}=?" for k in data)
        with self._lock, self._conn() as conn:
            conn.execute(f"UPDATE jobs SET {sets} WHERE job_id=?",
                         (*data.values(), job_id))

    def progress(self, job_id: str, phase: str, pct: float,
                 message: str = "") -> None:
        self.update(job_id, phase=str(phase)[:60],
                    progress=round(float(pct), 4),
                    message=str(message)[:300])

    def mark_running(self, job_id: str, pid: int | None = None) -> None:
        job = self.get(job_id) or {}
        status = "INSTALLING" if job.get("kind") in ("install", "update",
                                                     "remove") else "RUNNING"
        self.update(job_id, status=status, started_at=time.time(), pid=pid,
                    attempts=self.attempts(job_id) + 1, phase="starting",
                    message="worker started")

    def attempts(self, job_id: str) -> int:
        return int((self.get(job_id) or {}).get("attempts") or 0)

    def finish(self, job_id: str, result: dict[str, Any]) -> str:
        """Persist the outcome unless the user already cancelled the job."""
        current = self.get(job_id) or {}
        if str(current.get("status")) == "CANCELLED":
            return "CANCELLED"
        state = state_for_result(result)
        self.update(
            job_id, status=state, finished_at=time.time(),
            progress=1.0 if state == "COMPLETED" else
                (current.get("progress") or 0.0),
            phase=("done" if state == "COMPLETED" else
                   "needs configuration" if state == "NEEDS_CONFIGURATION" else
                   "awaiting approval" if state == "WAITING_FOR_APPROVAL" else
                   "cancelled" if state == "CANCELLED" else "failed"),
            message=str(result.get("message") or result.get("headline")
                        or "")[:300],
            outputs=result.get("outputs") or [],
            runtime_s=result.get("runtime_s"),
            ram_peak_mb=result.get("ram_peak_mb"),
            # 4000, not 500: a truncated provider error hid the real
            # diagnosis for an hour (2026-10-24 — " connections: [deque..."
            # instead of Google's free-tier limit:0 quota body). 4000 chars
            # still fits comfortably in the SQLite row and the RIO card.
            error=str(result.get("error") or "")[:4000]
            if state in ("FAILED", "CANCELLED") else "")
        return state

    # ── cancellation ─────────────────────────────────────────────────────
    def is_cancelled(self, job_id: str) -> bool:
        job = self.get(job_id) or {}
        return bool(job.get("cancel_requested")) or \
            str(job.get("status")) == "CANCELLED"

    def cancel(self, job_id: str, reason: str = "cancelled by user") -> dict:
        job = self.get(job_id)
        if not job:
            return {"ok": False, "error": f"unknown job {job_id}"}
        if job["status"] not in ACTIVE and job["status"] not in HOLD:
            return {"ok": False, "error": f"job is {job['status']}"}
        self.update(job_id, cancel_requested=1, status="CANCELLED",
                    finished_at=time.time(), phase="cancelled",
                    message=reason, error=reason)
        pid = job.get("pid")
        signal_sent = bool(pid and _terminate(int(pid)))
        return {"ok": True, "job_id": job_id, "signal_sent": signal_sent,
                "reason": reason}

    # ── restart recovery + bounded concurrency ──────────────────────────
    def recover_stale(self) -> list[dict]:
        """Mark active rows whose worker process is gone as FAILED."""
        recovered = []
        for job in self.list(limit=200, status="RUNNING") + \
                self.list(limit=200, status="INSTALLING"):
            pid = job.get("pid")
            if pid and _alive(int(pid)):
                continue
            self.update(job["job_id"], status="FAILED",
                        finished_at=time.time(), phase="recovered",
                        error="worker process disappeared (restart recovery)",
                        message="interrupted — start the job again to retry")
            recovered.append({"job_id": job["job_id"], "kind": job["kind"],
                              "ref": job["ref"]})
        return recovered

    def dispatch_queued(self) -> list[str]:
        """Spawn QUEUED workers while concurrency slots are free (oldest first)."""
        cfg = config()
        spawned: list[str] = []
        while self.count_active() < int(cfg["max_concurrent"]):
            with self._conn() as conn:
                row = conn.execute(
                    "SELECT job_id FROM jobs WHERE status='QUEUED' "
                    "ORDER BY created_at ASC LIMIT 1").fetchone()
            if not row:
                break
            job_id = row["job_id"]
            result = spawn_worker(job_id)
            if result.get("ok"):
                spawned.append(job_id)
            else:
                self.update(job_id, status="FAILED", finished_at=time.time(),
                            error=result.get("error"),
                            message="could not start worker")
                break
        return spawned

    # ── notification dedup ───────────────────────────────────────────────
    def claim_notification(self, dedup_key: str, *, event: str = "",
                           job_id: str = "", channel: str = "hermes") -> bool:
        now = time.time()
        with self._lock, self._conn() as conn:
            row = conn.execute(
                "SELECT created_at FROM notifications WHERE dedup_key=?",
                (dedup_key,)).fetchone()
            if row and now - float(row["created_at"]) < 12 * 3600:
                return False
            conn.execute(
                "INSERT INTO notifications (dedup_key, created_at, event,"
                " job_id, channel) VALUES (?,?,?,?,?) "
                "ON CONFLICT(dedup_key) DO UPDATE SET created_at=excluded.created_at,"
                " event=excluded.event, job_id=excluded.job_id, delivered=0",
                (dedup_key, now, event, job_id, channel))
            return True

    def mark_notified(self, dedup_key: str, delivered: bool,
                      detail: str = "") -> None:
        with self._lock, self._conn() as conn:
            conn.execute("UPDATE notifications SET delivered=?, detail=? "
                         "WHERE dedup_key=?",
                         (1 if delivered else 0, str(detail)[:300], dedup_key))

    # ── kv ───────────────────────────────────────────────────────────────
    def kv_get(self, key: str, default: Any = None) -> Any:
        with self._conn() as conn:
            row = conn.execute("SELECT v FROM kv WHERE k=?", (key,)).fetchone()
        if not row:
            return default
        try:
            return json.loads(row["v"])
        except Exception:
            return default

    def kv_set(self, key: str, value: Any) -> None:
        with self._lock, self._conn() as conn:
            conn.execute(
                "INSERT INTO kv (k, v, updated_at) VALUES (?,?,?) "
                "ON CONFLICT(k) DO UPDATE SET v=excluded.v,"
                " updated_at=excluded.updated_at",
                (key, json.dumps(value, default=str), time.time()))


# ── process helpers (same contract as the backtest store) ───────────────────

def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False
    except Exception:
        return False


def _terminate(pid: int, grace_s: float = 5.0) -> bool:
    try:
        os.killpg(os.getpgid(pid), signal.SIGTERM)
    except Exception:
        try:
            os.kill(pid, signal.SIGTERM)
        except Exception:
            return False
    deadline = time.time() + grace_s
    while time.time() < deadline:
        if not _alive(pid):
            return True
        time.sleep(0.2)
    try:
        os.killpg(os.getpgid(pid), signal.SIGKILL)
    except Exception:
        try:
            os.kill(pid, signal.SIGKILL)
        except Exception:
            pass
    return True


def spawn_worker(job_id: str) -> dict[str, Any]:
    """Start the detached worker for a queued job. Returns {ok, pid, log}.

    Idempotent by state: only a QUEUED job can be spawned — a second spawn
    attempt (race between create() and start_job(), or two concurrent
    dispatchers) is refused instead of double-running the work."""
    if not WORKER.exists():
        return {"ok": False, "error": f"worker script missing: {WORKER}"}
    store = RepoJobStore()
    job = store.get(job_id)
    if not job:
        return {"ok": False, "error": f"unknown job {job_id}"}
    if str(job.get("status")) != "QUEUED":
        return {"ok": True, "already_running": True,
                "status": job.get("status")}
    log = logs_dir() / f"{job_id}.log"
    env = dict(os.environ)
    env["JARVIS_REPO_AGENT_JOB"] = job_id
    env.setdefault("PYTHONUNBUFFERED", "1")
    try:
        with open(log, "ab") as fh:
            fh.write(f"\n===== worker start {time.strftime('%F %T')} =====\n"
                     .encode())
            fh.flush()
            proc = subprocess.Popen(
                [sys.executable, str(WORKER), "--worker", job_id],
                cwd=str(ROOT), env=env, stdout=fh, stderr=subprocess.STDOUT,
                start_new_session=True)
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    RepoJobStore().update(job_id, pid=proc.pid, log_path=str(log))
    return {"ok": True, "pid": proc.pid, "log": str(log)}


def start_job(kind: str, ref: str, *, source: str = "cli",
              params: dict | None = None) -> dict[str, Any]:
    """Recover stale rows → create (or dedup) → spawn. Never blocks."""
    store = RepoJobStore()
    store.recover_stale()
    created = store.create(kind, ref, source=source, params=params)
    if created.get("deduplicated"):
        return {"ok": True, "deduplicated": True, "job": created["job"],
                "job_id": created["job_id"], "status": created["job"]["status"]}
    job_id = created["job_id"]
    spawned = spawn_worker(job_id)
    if not spawned.get("ok"):
        store.update(job_id, status="FAILED", finished_at=time.time(),
                     error=spawned.get("error"),
                     message="could not start worker")
        return {"ok": False, "error": spawned.get("error"), "job_id": job_id}
    return {"ok": True, "deduplicated": False, "job_id": job_id,
            "job": store.get(job_id), "pid": spawned.get("pid"),
            "log": spawned.get("log")}


def is_transient(reason: str) -> bool:
    text = str(reason or "").lower()
    return any(hint in text for hint in TRANSIENT_HINTS)


def state_for_result(result: dict[str, Any] | None) -> str:
    """Map a worker result to its terminal state — the exact mapping
    RepoJobStore.finish() applies. Exported so the detached worker can write
    the result file BEFORE the job row goes terminal without putting a
    placeholder state in it (a reader that sees COMPLETED in the DB must
    never open the result file and find PENDING in it)."""
    raw = str((result or {}).get("status") or "FAILED").upper()
    if raw in ("COMPLETED", "FAILED", "CANCELLED",
               "NEEDS_CONFIGURATION", "WAITING_FOR_APPROVAL"):
        return raw
    if raw in ("OK", "SUCCESS", "READY", "PARTIAL"):
        return "COMPLETED"          # install finished; repo may be READY
    return "FAILED"


def _sig(params: dict) -> str:
    import hashlib
    return hashlib.sha1(
        json.dumps(params, sort_keys=True, default=str)
        .encode("utf-8")).hexdigest()[:12]
