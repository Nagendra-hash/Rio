"""
orchestrator/agents/backtest/jobs.py — durable job state, dedup, cancellation.

States: QUEUED → RUNNING → COMPLETED | FAILED | CANCELLED

Long jobs run in a SEPARATE process (scripts/backtest_run.py --worker), so the
Jarvis conversation loop, the dashboard and Hermes never block. The queue is
SQLite (stdlib, one file, WAL) under memory/backtest/backtest.db.

Guarantees
    * unique job ids (bt-YYYYMMDD-hex10)
    * dedup: an active job with the same (symbol, market, mode, parameters) is
      returned instead of starting a second one; a very recent identical
      completion is reused too. Different parameters (timeframes, strategies,
      history depth) are a different job — changing a selector and pressing
      Run again in the Backtest room must actually run.
    * bounded retries: at most `max_attempts` (default 2), only for transient
      data/network failures
    * clean cancellation: flag + SIGTERM to the worker's process group, then a
      short grace period before SIGKILL
    * restart recovery: a RUNNING row whose pid is gone is marked FAILED with a
      reason, and can be re-queued explicitly (never silently duplicated)
"""
from __future__ import annotations

import json
import hashlib
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

from orchestrator.agents.backtest.config import Config, default_config
from orchestrator.agents.backtest.store import db_path, logs_dir, now_iso

STATES = ("QUEUED", "RUNNING", "COMPLETED", "FAILED", "CANCELLED")
ACTIVE = ("QUEUED", "RUNNING")
TRANSIENT_HINTS = ("timed out", "timeout", "connection", "429", "rate limit",
                   "temporarily", "network", "HTTP 5", "reset by peer")

ROOT = Path(__file__).resolve().parents[3]
WORKER = ROOT / "scripts" / "backtest_run.py"


def new_job_id() -> str:
    return f"bt-{datetime.now(timezone.utc).strftime('%Y%m%d')}-{uuid.uuid4().hex[:10]}"


class JobStore:
    def __init__(self, path: str | Path | None = None,
                 cfg: Config | None = None) -> None:
        self.path = Path(path) if path else db_path()
        self.cfg = cfg or default_config()
        self._lock = threading.RLock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
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
                    symbol TEXT NOT NULL,
                    market TEXT DEFAULT '',
                    mode TEXT DEFAULT 'full',
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
                    report_dir TEXT DEFAULT '',
                    error TEXT DEFAULT '',
                    runtime_s REAL,
                    ram_peak_mb REAL,
                    experiments INTEGER DEFAULT 0,
                    result_status TEXT DEFAULT '',
                    headline TEXT DEFAULT '',
                    champion TEXT DEFAULT '',
                    oos TEXT DEFAULT '',
                    dedup_key TEXT DEFAULT '',
                    cancel_requested INTEGER DEFAULT 0,
                    options TEXT DEFAULT '{}'
                )""")
            conn.execute("""CREATE INDEX IF NOT EXISTS idx_jobs_status
                            ON jobs(status, created_at)""")
            conn.execute("""CREATE INDEX IF NOT EXISTS idx_jobs_dedup
                            ON jobs(dedup_key, status)""")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS notifications (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    dedup_key TEXT UNIQUE,
                    created_at REAL NOT NULL,
                    event TEXT DEFAULT '',
                    job_id TEXT DEFAULT '',
                    channel TEXT DEFAULT '',
                    delivered INTEGER DEFAULT 0,
                    detail TEXT DEFAULT ''
                )""")
            conn.execute("""CREATE TABLE IF NOT EXISTS kv (
                            k TEXT PRIMARY KEY, v TEXT, updated_at REAL)""")

    # ── create / dedup ───────────────────────────────────────────────────
    def create(self, symbol: str, market: str | None = None, *,
               mode: str = "full", source: str = "cli",
               options: dict | None = None,
               dedup_window_s: float | None = None) -> dict[str, Any]:
        symbol = str(symbol or "").strip().upper()
        market = (market or "").strip().lower()
        # Parameters are part of the identity: "BTC spot" with 1 year of 2h
        # candles is not the same job as "BTC spot" with 5 years of 1d.
        opt_sig = hashlib.sha1(
            json.dumps(options or {}, sort_keys=True, default=str)
            .encode("utf-8")).hexdigest()[:12]
        dedup_key = f"{symbol}:{market or 'auto'}:{mode}:{opt_sig}"
        window = float(dedup_window_s if dedup_window_s is not None else
                       self.cfg.get("dedup_window_s", 300))
        now = time.time()
        with self._lock, self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM jobs WHERE dedup_key=? AND status IN "
                "('QUEUED','RUNNING') ORDER BY created_at DESC LIMIT 1",
                (dedup_key,)).fetchall()
            if rows:
                job = dict(rows[0])
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
            conn.execute(
                "INSERT INTO jobs (job_id, symbol, market, mode, source, status,"
                " created_at, dedup_key, options, phase, message)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (job_id, symbol, market, mode, source, "QUEUED", now,
                 dedup_key, json.dumps(options or {}), "queued",
                 "waiting for a worker"))
            job = dict(conn.execute("SELECT * FROM jobs WHERE job_id=?",
                                    (job_id,)).fetchone())
        return {"ok": True, "deduplicated": False, "job": job, "job_id": job_id}

    # ── reads ────────────────────────────────────────────────────────────
    def get(self, job_id: str) -> dict | None:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM jobs WHERE job_id=?",
                               (job_id,)).fetchone()
        return dict(row) if row else None

    def list(self, limit: int = 20, status: str | None = None,
             symbol: str | None = None) -> list[dict]:
        query = "SELECT * FROM jobs WHERE 1=1"
        args: list[Any] = []
        if status:
            query += " AND status=?"
            args.append(status.upper())
        if symbol:
            query += " AND symbol=?"
            args.append(symbol.upper())
        query += " ORDER BY created_at DESC LIMIT ?"
        args.append(int(limit))
        with self._conn() as conn:
            return [dict(r) for r in conn.execute(query, args).fetchall()]

    # ── updates ──────────────────────────────────────────────────────────
    def update(self, job_id: str, **fields: Any) -> None:
        if not fields:
            return
        allowed = {"status", "started_at", "finished_at", "pid", "attempts",
                   "progress", "phase", "message", "report_dir", "error",
                   "runtime_s", "ram_peak_mb", "experiments", "result_status",
                   "headline", "champion", "oos", "cancel_requested",
                   "market", "options"}
        data = {k: v for k, v in fields.items() if k in allowed}
        if not data:
            return
        for key in ("champion", "oos", "options"):
            if key in data and not isinstance(data[key], str):
                data[key] = json.dumps(data[key], default=str)
        sets = ", ".join(f"{k}=?" for k in data)
        with self._lock, self._conn() as conn:
            conn.execute(f"UPDATE jobs SET {sets} WHERE job_id=?",
                         (*data.values(), job_id))

    def progress(self, job_id: str, phase: str, pct: float,
                 message: str = "") -> None:
        self.update(job_id, phase=phase, progress=round(float(pct), 4),
                    message=str(message)[:300])

    def mark_running(self, job_id: str, pid: int | None = None) -> None:
        self.update(job_id, status="RUNNING", started_at=time.time(), pid=pid,
                    attempts=self.attempts(job_id) + 1, phase="starting",
                    message="worker started")

    def attempts(self, job_id: str) -> int:
        job = self.get(job_id) or {}
        return int(job.get("attempts") or 0)

    def finish(self, job_id: str, result: dict[str, Any]) -> str:
        """Persist the outcome unless the user already cancelled the job."""
        current = self.get(job_id) or {}
        if str(current.get("status")) == "CANCELLED":
            return "CANCELLED"
        status = str(result.get("status") or "FAILED")
        state = {"COMPLETED": "COMPLETED",
                 "NO_ROBUST_STRATEGY_FOUND": "COMPLETED",
                 "INSUFFICIENT_DATA": "FAILED",
                 "BLOCKED": "FAILED",
                 "NEEDS_INPUT": "COMPLETED",
                 "CANCELLED": "CANCELLED",
                 "FAILED": "FAILED"}.get(status, "FAILED")
        champ = result.get("champion") or {}
        self.update(
            job_id, status=state, finished_at=time.time(),
            progress=1.0 if state == "COMPLETED" else 0.0,
            phase=("done" if state == "COMPLETED" else
                   "cancelled" if state == "CANCELLED" else "failed"),
            message=str(result.get("headline") or result.get("reason") or "")[:300],
            result_status=status,
            headline=str(result.get("headline") or "")[:300],
            champion={k: champ.get(k) for k in
                      ("strategy", "version", "timeframe", "params",
                       "oos_score")} if champ else {},
            oos=(champ.get("oos") or {}) if champ else {},
            runtime_s=result.get("runtime_s"),
            ram_peak_mb=result.get("ram_peak_mb"),
            experiments=int(result.get("experiments_total") or 0),
            error=str(result.get("reason") or "")[:500]
            if state == "FAILED" else "")
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
        if job["status"] not in ACTIVE:
            return {"ok": False, "error": f"job is {job['status']}"}
        self.update(job_id, cancel_requested=1, status="CANCELLED",
                    finished_at=time.time(), message=reason, phase="cancelled",
                    error=reason)
        pid = job.get("pid")
        terminated = False
        if pid:
            terminated = _terminate(int(pid))
        return {"ok": True, "job_id": job_id, "signal_sent": terminated,
                "reason": reason}

    # ── restart recovery ─────────────────────────────────────────────────
    def recover_stale(self) -> list[dict]:
        """Mark RUNNING rows whose process is gone as FAILED (recoverable)."""
        recovered = []
        for job in self.list(limit=200, status="RUNNING"):
            pid = job.get("pid")
            if pid and _alive(int(pid)):
                continue
            self.update(job["job_id"], status="FAILED", finished_at=time.time(),
                        phase="recovered",
                        error="worker process disappeared (restart recovery)",
                        message="interrupted — re-run to continue")
            recovered.append({"job_id": job["job_id"], "symbol": job["symbol"],
                              "attempts": job.get("attempts")})
        return recovered

    def requeue(self, job_id: str) -> dict:
        job = self.get(job_id)
        if not job:
            return {"ok": False, "error": f"unknown job {job_id}"}
        if job["status"] in ACTIVE:
            return {"ok": False, "error": "job is already active"}
        if int(job.get("attempts") or 0) >= int(self.cfg.get("max_attempts", 2)):
            return {"ok": False, "error": "retry budget exhausted"}
        self.update(job_id, status="QUEUED", started_at=None, finished_at=None,
                    pid=None, progress=0.0, cancel_requested=0,
                    phase="queued", message="re-queued", error="")
        return {"ok": True, "job_id": job_id}

    # ── notification dedup ───────────────────────────────────────────────
    def claim_notification(self, dedup_key: str, *, event: str = "",
                           job_id: str = "", channel: str = "",
                           hours: float = 12.0) -> bool:
        """True when this notification has not been sent in the window."""
        now = time.time()
        with self._lock, self._conn() as conn:
            row = conn.execute(
                "SELECT created_at FROM notifications WHERE dedup_key=?",
                (dedup_key,)).fetchone()
            if row and now - float(row["created_at"]) < hours * 3600:
                return False
            conn.execute(
                "INSERT INTO notifications (dedup_key, created_at, event, job_id,"
                " channel) VALUES (?,?,?,?,?) "
                "ON CONFLICT(dedup_key) DO UPDATE SET created_at=excluded.created_at,"
                " event=excluded.event, job_id=excluded.job_id,"
                " channel=excluded.channel, delivered=0",
                (dedup_key, now, event, job_id, channel))
            return True

    def mark_notified(self, dedup_key: str, delivered: bool,
                      detail: str = "") -> None:
        with self._lock, self._conn() as conn:
            conn.execute("UPDATE notifications SET delivered=?, detail=? "
                         "WHERE dedup_key=?", (1 if delivered else 0,
                                               str(detail)[:300], dedup_key))

    # ── kv (scheduler state) ─────────────────────────────────────────────
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
            conn.execute("INSERT INTO kv (k, v, updated_at) VALUES (?,?,?) "
                         "ON CONFLICT(k) DO UPDATE SET v=excluded.v,"
                         " updated_at=excluded.updated_at",
                         (key, json.dumps(value, default=str), time.time()))


# ── process helpers ──────────────────────────────────────────────────────────

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


def spawn_worker(job_id: str, cfg: Config | None = None) -> dict[str, Any]:
    """Start the detached research worker. Returns {ok, pid, log}."""
    cfg = cfg or default_config()
    if not WORKER.exists():
        return {"ok": False, "error": f"worker script missing: {WORKER}"}
    log = logs_dir() / f"{job_id}.log"
    env = dict(os.environ)
    env["JARVIS_BACKTEST_JOB"] = job_id
    env.setdefault("PYTHONUNBUFFERED", "1")
    try:
        with open(log, "ab") as fh:
            proc = subprocess.Popen(
                [sys.executable, str(WORKER), "--worker", job_id],
                cwd=str(ROOT), env=env, stdout=fh, stderr=subprocess.STDOUT,
                start_new_session=True)
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    JobStore(cfg=cfg).update(job_id, pid=proc.pid)
    return {"ok": True, "pid": proc.pid, "log": str(log)}


def start_job(symbol: str, market: str | None = None, *, mode: str = "full",
              source: str = "cli", options: dict | None = None,
              cfg: Config | None = None) -> dict[str, Any]:
    """Create (or dedup) a job and spawn its worker."""
    cfg = cfg or default_config()
    store = JobStore(cfg=cfg)
    store.recover_stale()
    created = store.create(symbol, market, mode=mode, source=source,
                           options=options)
    if not created.get("ok"):
        return created
    job = created["job"]
    if created.get("deduplicated"):
        return {"ok": True, "deduplicated": True, "job": job,
                "job_id": job["job_id"], "status": job["status"]}
    spawned = spawn_worker(job["job_id"], cfg)
    if not spawned.get("ok"):
        store.update(job["job_id"], status="FAILED", error=spawned.get("error"),
                     message="could not start worker")
        return {"ok": False, "error": spawned.get("error"),
                "job_id": job["job_id"]}
    return {"ok": True, "deduplicated": False,
            "job": store.get(job["job_id"]), "job_id": job["job_id"],
            "pid": spawned.get("pid"), "log": spawned.get("log")}


def is_transient(reason: str) -> bool:
    text = str(reason or "").lower()
    return any(hint in text for hint in TRANSIENT_HINTS)


def maybe_retry(job_id: str, result: dict[str, Any],
                cfg: Config | None = None) -> dict[str, Any]:
    """Re-queue once for a transient failure, within the retry budget."""
    cfg = cfg or default_config()
    store = JobStore(cfg=cfg)
    job = store.get(job_id) or {}
    if str(result.get("status")) != "FAILED":
        return {"retried": False, "reason": "not a failure"}
    if not is_transient(result.get("reason") or result.get("error") or ""):
        return {"retried": False, "reason": "not a transient error"}
    if int(job.get("attempts") or 0) >= int(cfg.get("max_attempts", 2)):
        return {"retried": False, "reason": "retry budget exhausted"}
    store.requeue(job_id)
    return {"retried": True, "spawn": spawn_worker(job_id, cfg)}
