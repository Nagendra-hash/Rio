"""Tests for the Live Ops payload + route (Phase: agent theatre).

Covers:
  * _collect_ops parsing of the orchestrator journal — plan/task timings,
    step counts, retries, stalled runs, KPI roll-up
  * in-flight agent runs from _RUNNING
  * /api/rio/ops auth contract + /ops shell route
  * no simulation: a run without a terminal event is never "completed"
"""
import sys
import time
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import dashboard.rio_api as rio_api  # noqa: E402


def _iso(ts: str) -> str:
    return ts


AGENTS_PAYLOAD = {
    "ok": True,
    "orchestrator": {"available": True, "mode": "shadow", "registered_count": 2},
    "autonomy": {"available": True, "emergency_stop": False, "paused_agents": []},
    "agents": [
        {"key": "coding", "name": "Coding Agent", "registry": "coding_agent",
         "status": "RUNNING", "status_detail": "executing task t-1",
         "description": "writes code", "model": "deepseek", "impl": "real",
         "current_task": {"task_id": "t-1", "started": time.time() - 12,
                          "prompt": "fix it"},
         "last_execution": None, "stats": {"ok": 3, "fail": 1, "other": 0},
         "history": [], "actions": {"run": True}},
        {"key": "news", "name": "News Curator", "registry": "news_agent",
         "status": "NOT IMPLEMENTED", "status_detail": "no such agent",
         "description": "", "model": None, "impl": "absent",
         "current_task": None, "last_execution": None,
         "stats": {"ok": 0, "fail": 0, "other": 0},
         "history": [], "actions": {"run": False}},
    ],
}


EVENTS = [
    # completed 2-step plan, 20 s
    {"ts": "2026-10-20T10:00:00.000Z", "event": "plan.plan_started",
     "task_id": "plan-a", "step_id": "", "detail": "2 steps"},
    {"ts": "2026-10-20T10:00:01.000Z", "event": "plan.step_started",
     "task_id": "plan-a", "step_id": "s1", "detail": "attempt 1: do"},
    {"ts": "2026-10-20T10:00:09.000Z", "event": "plan.step_done",
     "task_id": "plan-a", "step_id": "s1", "detail": "done: one"},
    {"ts": "2026-10-20T10:00:10.000Z", "event": "plan.step_started",
     "task_id": "plan-a", "step_id": "s2", "detail": "attempt 1: more"},
    {"ts": "2026-10-20T10:00:20.000Z", "event": "plan.step_done",
     "task_id": "plan-a", "step_id": "s2", "detail": "done: two"},
    {"ts": "2026-10-20T10:00:20.000Z", "event": "plan.plan_completed",
     "task_id": "plan-a", "step_id": "", "detail": "2 steps"},
    # failed plan after 10 s, with a retry
    {"ts": "2026-10-20T10:05:00.000Z", "event": "plan.plan_started",
     "task_id": "plan-b", "step_id": "", "detail": "1 step"},
    {"ts": "2026-10-20T10:05:02.000Z", "event": "plan.step_retry",
     "task_id": "plan-b", "step_id": "s1", "detail": "attempt 1 failed"},
    {"ts": "2026-10-20T10:05:09.000Z", "event": "plan.step_failed",
     "task_id": "plan-b", "step_id": "s1", "detail": "agent exploded"},
    {"ts": "2026-10-20T10:05:10.000Z", "event": "plan.plan_failed",
     "task_id": "plan-b", "step_id": "", "detail": "1 step"},
    # routed task, 3 s
    {"ts": "2026-10-20T10:10:00.000Z", "event": "task.received",
     "task_id": "task-c", "channel": "", "text": "status please"},
    {"ts": "2026-10-20T10:10:00.100Z", "event": "task.classified",
     "task_id": "task-c", "task_type": "system", "confidence": 0.9,
     "reason": "rule"},
    {"ts": "2026-10-20T10:10:03.000Z", "event": "task.agent_result",
     "task_id": "task-c", "agent": "system_agent", "status": "completed",
     "detail": "all good"},
    # no terminal event → must be stalled (old run), never completed
    {"ts": "2026-10-20T09:00:00.000Z", "event": "plan.plan_started",
     "task_id": "plan-stale", "step_id": "", "detail": "3 steps"},
    {"ts": "2026-10-20T09:00:01.000Z", "event": "plan.step_started",
     "task_id": "plan-stale", "step_id": "s1", "detail": "attempt 1: work"},
]


@pytest.fixture()
def ops(monkeypatch):
    monkeypatch.setattr(rio_api, "_collect_agents", lambda: dict(AGENTS_PAYLOAD))
    monkeypatch.setattr(rio_api, "_tail_jsonl",
                        lambda *a, **k: [dict(e) for e in EVENTS])
    monkeypatch.setattr(rio_api, "_journal_path", lambda orch: Path("/tmp/j.jsonl"))
    # isolate autonomy + in-flight state
    import dashboard.autonomy_api as auto
    monkeypatch.setattr(auto, "shared_engine", lambda: None)
    monkeypatch.setattr(rio_api, "_RUNNING", {})
    return rio_api._collect_ops()


def test_plan_timings_and_steps(ops):
    by_id = {r["task_id"]: r for r in ops["runs"]}
    a = by_id["plan-a"]
    assert a["kind"] == "plan"
    assert a["status"] == "completed"
    assert a["duration_s"] == 20.0
    assert a["step_count"] == 2
    assert a["steps_done"] == 2
    b = by_id["plan-b"]
    assert b["status"] == "failed"
    assert b["duration_s"] == 10.0
    assert b["retries"] == 1
    assert b["steps_failed"] == 1


def test_routed_task_duration_and_agent(ops):
    c = {r["task_id"]: r for r in ops["runs"]}["task-c"]
    assert c["kind"] == "task"
    assert c["status"] == "completed"
    assert c["agent"] == "system_agent"
    assert c["duration_s"] == 3.0


def test_unfinished_run_is_never_completed(ops):
    stale = {r["task_id"]: r for r in ops["runs"]}["plan-stale"]
    assert stale["status"] in ("stalled", "running")
    assert stale["status"] != "completed"


def test_kpis_terminal_only(ops):
    k = ops["kpis"]
    assert k["runs_completed"] == 2          # plan-a + task-c
    assert k["runs_failed"] == 1             # plan-b
    assert k["success_rate"] == 66.7
    assert k["avg_duration_s"] == 11.0       # (20 + 10 + 3) / 3
    assert k["p95_duration_s"] == 19.0
    assert k["agents_total"] == 2
    assert k["agents_running"] == 1
    assert k["agents_not_implemented"] == 1


def test_feed_newest_first_and_bounded(ops):
    feed = ops["feed"]
    assert feed, "feed must contain journal events"
    ages = [f["age_s"] for f in feed if f["age_s"] is not None]
    assert ages == sorted(ages), "feed must be newest-first"
    assert len(feed) <= 50


def test_in_flight_reported(monkeypatch):
    monkeypatch.setattr(rio_api, "_collect_agents", lambda: dict(AGENTS_PAYLOAD))
    monkeypatch.setattr(rio_api, "_tail_jsonl", lambda *a, **k: [])
    monkeypatch.setattr(rio_api, "_journal_path", lambda orch: Path("/tmp/j.jsonl"))
    import dashboard.autonomy_api as auto
    monkeypatch.setattr(auto, "shared_engine", lambda: None)
    monkeypatch.setattr(rio_api, "_RUNNING", {
        "coding_agent": {"task_id": "t-live",
                         "started": time.time() - 5, "prompt": "work"},
    })
    out = rio_api._collect_ops()
    assert out["kpis"]["in_flight"] == 1
    assert out["in_flight"][0]["task_id"] == "t-live"
    assert out["in_flight"][0]["elapsed_s"] >= 4


# ── HTTP contract ──────────────────────────────────────────────────────────

def _app(auth=True):
    app = FastAPI()
    rio_api.register(app, lambda request: auth)
    return TestClient(app)


def test_ops_endpoint_requires_auth(monkeypatch):
    monkeypatch.setattr(rio_api, "_collect_ops", lambda: {"ok": True, "runs": []})
    c = _app(auth=False)
    assert c.get("/api/rio/ops").status_code == 401
    assert c.get("/api/rio/ops", headers={"Authorization": "Bearer nope"}).status_code == 401


def test_ops_endpoint_payload(monkeypatch):
    monkeypatch.setattr(rio_api, "_collect_ops", lambda: {"ok": True, "kpis": {"agents_total": 2}})
    c = _app()
    r = c.get("/api/rio/ops", headers={"Authorization": "Bearer test"})
    assert r.status_code == 200
    assert r.json()["kpis"]["agents_total"] == 2


def test_ops_shell_route_and_asset():
    c = _app()
    r = c.get("/ops")
    assert r.status_code == 200
    assert "text/html" in r.headers.get("content-type", "")
    assert "ops.js" in r.text
    a = c.get("/rio/assets/ops.js")
    assert a.status_code == 200
    assert "application/javascript" in a.headers.get("content-type", "")
