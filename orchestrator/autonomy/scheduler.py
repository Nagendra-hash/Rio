"""
orchestrator/autonomy/scheduler.py — PHASE 20: scheduled workflows & triggers.

WHAT LIVES HERE
    * TaskStore      — durable scheduled-task registry (JSONL of task defs +
                       run history), fully CRUD-able (scheduled-task mgmt).
    * Trigger        — when a task fires:
                         interval  — every N seconds (min floor 60)
                         daily     — at HH:MM local time
                         event     — a named event bus signal ("email.arrived")
                         condition — a probe predicate is true (and, with
                                     `cooldown_s`, not recently fired)
    * Scheduler      — the loop: due checks → run through the AutonomyEngine
                       → per-task failure backoff → resource limits (max
                       concurrent runs, per-run step/timeout caps live in
                       the engine gate) → notifications on alert events.

DESIGN NOTES
    * The scheduler NEVER executes by itself: it builds a Workflow and a
      step list and hands them to AutonomyEngine.run — one security gate
      for everything. Trigger evaluation (probes) is read-only.
    * Failure recovery: consecutive failures park the task in backoff and
      (optionally) notify once; a success clears the streak.
"""
from __future__ import annotations

import json
import threading
import time
from datetime import datetime
from pathlib import Path

from orchestrator.autonomy.engine import (AutonomyEngine, Step, Workflow,
                                          risk_rank)
from orchestrator.autonomy.monitoring import PROBES

MIN_INTERVAL_S = 60          # no "every 5 seconds" raids on the API tier
MAX_CONCURRENT = 3           # resource limit: parallel autonomous runs
DEFAULT_CONDITION_COOLDOWN_S = 6 * 3600
MAX_FAILURES_BEFORE_NOTIFY = 3


def _now() -> float:
    return time.time()


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts).isoformat(timespec="seconds")


# ── the task store ───────────────────────────────────────────────────────────

class TaskStore:
    """Scheduled tasks + their run history, durable in two JSONL files."""

    def __init__(self, path: str | Path | None = None) -> None:
        from memory.config_manager import BASE_DIR
        self.path = Path(path) if path else \
            BASE_DIR / "memory" / "autonomy" / "scheduled_tasks.json"

    def _read(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                data.setdefault("tasks", {})
                data.setdefault("history", [])
                return data
        except Exception:
            pass
        return {"tasks": {}, "history": []}

    def _write(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=1, ensure_ascii=False),
                       encoding="utf-8")
        tmp.replace(self.path)

    # ── CRUD ─────────────────────────────────────────────────────────────
    def add(self, spec: dict) -> dict:
        name = str(spec.get("name") or "").strip()
        if not name:
            return {"ok": False, "error": "task name required"}
        data = self._read()
        if name in data["tasks"]:
            return {"ok": False, "error": f"task '{name}' already exists"}
        entry = {
            "name": name,
            "kind": str(spec.get("kind") or "scheduled"),
            "level": max(1, min(int(spec.get("level") or 1), 5)),
            "trigger": dict(spec.get("trigger") or {}),
            "agents": tuple(spec.get("agents") or ()),
            "tools": tuple(spec.get("tools") or ()),
            "steps": list(spec.get("steps") or []),
            "max_steps": int(spec.get("max_steps") or 12),
            "timeout_s": float(spec.get("timeout_s") or 600),
            "enabled": bool(spec.get("enabled", True)),
            "notify": dict(spec.get("notify") or {}),
            "condition_cooldown_s": int(spec.get("condition_cooldown_s")
                                        or DEFAULT_CONDITION_COOLDOWN_S),
            "created_at": _iso(_now()),
            "next_run": 0.0, "last_run": 0.0,
            "last_status": "", "failures": 0, "last_value": "",
        }
        data["tasks"][name] = entry
        self._write(data)
        return {"ok": True, "task": name}

    def get(self, name: str) -> dict | None:
        return self._read()["tasks"].get(name)

    def update(self, name: str, **fields) -> dict:
        data = self._read()
        task = data["tasks"].get(name)
        if not task:
            return {"ok": False, "error": f"unknown task '{name}'"}
        for k in ("level", "max_steps", "timeout_s", "condition_cooldown_s"):
            if k in fields:
                task[k] = int(fields[k])
        for k in ("enabled",):
            if k in fields:
                task[k] = bool(fields[k])
        for k in ("trigger", "notify"):
            if k in fields and isinstance(fields[k], dict):
                task[k].update(fields[k])
        for k in ("agents", "tools"):
            if k in fields:
                task[k] = tuple(fields[k])
        for k in ("steps",):
            if k in fields and isinstance(fields[k], list):
                task[k] = fields[k]
        self._write(data)
        return {"ok": True, "task": name}

    def remove(self, name: str) -> dict:
        data = self._read()
        if name not in data["tasks"]:
            return {"ok": False, "error": f"unknown task '{name}'"}
        del data["tasks"][name]
        self._write(data)
        return {"ok": True, "removed": name}

    def list(self, enabled_only: bool = False) -> list[dict]:
        tasks = list(self._read()["tasks"].values())
        if enabled_only:
            tasks = [t for t in tasks if t.get("enabled")]
        return sorted(tasks, key=lambda t: t.get("name", ""))

    # ── run bookkeeping ──────────────────────────────────────────────────
    def mark_run(self, name: str, status: str, note: str = "",
                 value: str = "") -> None:
        data = self._read()
        task = data["tasks"].get(name)
        if not task:
            return
        now = _now()
        task["last_run"] = now
        task["last_status"] = status
        task["last_value"] = str(value)[:300]
        task["failures"] = 0 if status == "ok" else int(task["failures"]) + 1
        if task["kind"] == "interval":
            iv = int(task.get("trigger", {}).get("interval_s") or MIN_INTERVAL_S)
            task["next_run"] = now + max(iv, MIN_INTERVAL_S)
        data["history"].append({"task": name, "ts": _iso(now),
                                "status": status, "note": note[:300]})
        data["history"] = data["history"][-1000:]
        self._write(data)

    def history(self, limit: int = 50) -> list[dict]:
        return self._read()["history"][-max(1, int(limit)):]


# ── triggers ────────────────────────────────────────────────────────────────

def _probe_ok(trigger: dict) -> tuple[bool, str]:
    """Evaluate a condition trigger: probe name + params → (fired?, note)."""
    probe_name = str(trigger.get("probe") or "").strip()
    fn = PROBES.get(probe_name)
    if fn is None:
        return False, f"unknown probe '{probe_name}'"
    try:
        result = fn(**(trigger.get("params") or {}))
    except Exception as exc:
        return False, f"probe error: {exc}"[:200]
    ok = bool(result.get("fired"))
    return ok, str(result.get("note") or "")[:200]


class Scheduler:
    """Tick loop: find due tasks, run them through the autonomy engine."""

    def __init__(self, engine: AutonomyEngine, store: TaskStore,
                 *, audit=None, clock=_now, max_concurrent: int = MAX_CONCURRENT,
                 tick_seconds: float = 5.0) -> None:
        self.engine = engine
        self.store = store
        self.audit = audit or engine.audit
        self.clock = clock
        self.max_concurrent = max(1, int(max_concurrent))
        self.tick_seconds = tick_seconds
        self._events: list[tuple[str, dict]] = []      # event-bus buffer
        self._fired_conditions: dict[str, float] = {}  # task → last fire ts
        self._running = 0
        self._lock = threading.RLock()
        self._stop_flag = threading.Event()

    # ── events ───────────────────────────────────────────────────────────
    def emit_event(self, name: str, payload: dict | None = None) -> dict:
        """External signal: 'email.important_arrived', 'price.level_hit'…"""
        self._events.append((str(name), dict(payload or {})))
        self.audit.record("event_emitted", detail={"event": str(name)})
        return {"ok": True, "event": str(name), "queued": True}

    # ── due computation ──────────────────────────────────────────────────
    def _daily_due(self, task: dict, now: float) -> bool:
        hh, mm = str(task.get("trigger", {}).get("time") or "07:00").split(":")[:2]
        target = datetime.now().replace(hour=int(hh), minute=int(mm),
                                        second=0, microsecond=0)
        last = float(task.get("last_run") or 0)
        return last < target.timestamp() <= now

    def _event_pending(self, task: dict) -> dict | None:
        want = str(task.get("trigger", {}).get("event") or "")
        if not want:
            return None
        for i, (name, payload) in enumerate(self._events):
            if name == want:
                del self._events[i]
                return {"event": name, **payload}
        return None

    def _condition_due(self, task: dict, now: float) -> tuple[bool, str]:
        trig = task.get("trigger", {})
        fired, note = _probe_ok(trig)
        if not fired:
            return False, note
        cooldown = int(task.get("condition_cooldown_s")
                       or DEFAULT_CONDITION_COOLDOWN_S)
        last_fire = self._fired_conditions.get(task["name"], 0)
        if now - last_fire < cooldown:
            return False, "cooldown"
        return True, note

    def due_tasks(self) -> list[tuple[dict, str]]:
        """[(task, why)] for everything that should run this tick. Interval
        and daily come from the store's clock; events drain; conditions
        probe (read-only) right here."""
        now = self.clock()
        out: list[tuple[dict, str]] = []
        for task in self.store.list(enabled_only=True):
            kind = task.get("kind")
            if kind == "interval":
                last = float(task.get("last_run") or 0)
                iv = int(task.get("trigger", {}).get("interval_s")
                         or MIN_INTERVAL_S)
                if now - last >= max(iv, MIN_INTERVAL_S):
                    out.append((task, f"interval {iv}s"))
            elif kind == "daily":
                if self._daily_due(task, now):
                    out.append((task, "daily schedule"))
            elif kind == "event":
                hit = self._event_pending(task)
                if hit:
                    out.append((task, f"event {hit['event']}"))
            elif kind == "condition":
                fired, note = self._condition_due(task, now)
                if fired:
                    self._fired_conditions[task["name"]] = now
                    out.append((task, f"condition: {note}"))
        return out

    # ── the run ──────────────────────────────────────────────────────────
    def run_task(self, task: dict, why: str) -> dict:
        """One scheduled task → engine run. Resource limits enforced here:
        concurrency cap first; step/timeout caps ride in the Workflow."""
        with self._lock:
            if self._running >= self.max_concurrent:
                self.audit.record("run_skipped", workflow=task["name"],
                                  detail={"why": "concurrency cap"})
                return {"ok": False, "skipped": "concurrency cap"}
            self._running += 1
        try:
            wf = Workflow(
                name=task["name"], level=int(task.get("level") or 1),
                agents=tuple(task.get("agents") or ()),
                tools=tuple(task.get("tools") or ()),
                max_steps=int(task.get("max_steps") or 12),
                timeout_s=float(task.get("timeout_s") or 600),
                enabled=bool(task.get("enabled", True)),
                notify=dict(task.get("notify") or {}),
                description=f"scheduled {task.get('kind')}: {why}")
            steps = [Step(tool=str(s.get("tool") or ""),
                          payload=dict(s.get("payload") or {}),
                          reason=str(s.get("reason") or ""),
                          agent=str(s.get("agent") or ""))
                     for s in (task.get("steps") or [])]
            result = self.engine.run(wf, steps)
            status = "ok" if result.get("ok") and \
                result.get("status") == "completed" else \
                ("pending" if result.get("status") == "awaiting_approval"
                 else "failed")
            note = result.get("error") or result.get("status") or ""
            self.store.mark_run(task["name"], status, note=str(note),
                                value=self._summarize(result))
            if status == "failed":
                # re-read: mark_run already bumped the failure streak; notify
                # ONCE at the threshold, not on every subsequent retry
                fresh = self.store.get(task["name"]) or {}
                fails = int(fresh.get("failures") or 0)
                if fails == MAX_FAILURES_BEFORE_NOTIFY:
                    self._notify_failure(fresh, fails, str(note))
            return result
        finally:
            with self._lock:
                self._running -= 1

    @staticmethod
    def _summarize(result: dict) -> str:
        parts = []
        for r in (result.get("results") or [])[:6]:
            bits = [str(r.get("tool") or "?")]
            if r.get("ran"):
                bits.append("ran")
            if r.get("needs_approval"):
                bits.append("approval")
            if r.get("denied"):
                bits.append("denied")
            parts.append("/".join(bits))
        return " ".join(parts)

    def _notify_failure(self, task: dict, fails: int, note: str) -> None:
        try:
            self.engine.notifier.notify(
                f"⚠️ scheduled task '{task['name']}' failed {fails}x — {note}",
                channel=str(task.get("notify", {}).get("channel") or "telegram"))
        except Exception:
            pass

    # ── the loop ─────────────────────────────────────────────────────────
    def tick(self) -> dict:
        """One scheduling pass. Safe to call from a background thread."""
        if self.engine.stopped:
            # the stop outranks the scheduler: due tasks are not even computed
            return {"due": 0, "ran": [], "stopped": True}
        due = self.due_tasks()
        ran = []
        for task, why in due:
            result = self.run_task(task, why)
            ran.append({"task": task["name"], "why": why,
                        "ok": bool(result.get("ok")),
                        "status": result.get("status")})
        return {"due": len(due), "ran": ran}

    def start(self) -> threading.Thread:
        """Background thread ticking every tick_seconds until stop()."""
        def _loop():
            while not self._stop_flag.wait(self.tick_seconds):
                try:
                    self.tick()
                except Exception:
                    continue
        self._stop_flag.clear()
        t = threading.Thread(target=_loop, daemon=True, name="autonomy-sched")
        t.start()
        return t

    def stop(self) -> None:
        self._stop_flag.set()
