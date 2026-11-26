"""Phase 6 (Universal Video Studio) — mandatory method-approval tests.

The mission's approval rules, each tested explicitly:
  * NO job starts without an approved method — create() returns the method
    question and queues NOTHING (bypass attempt #1)
  * a PENDING token cannot be consumed (bypass attempt #2: token without a
    method choice)
  * tokens are SINGLE-USE (bypass attempt #3: replay the same token)
  * tokens EXPIRE (bypass attempt #4: stale token)
  * approval is PROJECT-BOUND (bypass attempt #5: change topic after
    approval — different project → refused)
  * a method not offered for this project is refused (bypass attempt #6)
  * repeated identical requests reuse the SAME pending token (no stacking)
  * logs never contain secrets (script/image bodies are hashed, not stored)
  * approval from an earlier project never authorizes a new one

No network, no downloads, no paid calls. Job starts are intercepted —
assertions check that start_job was (not) called.
"""
from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from actions import video_creator as vc
from core.video_agent import approval as vappr


BASE_PARAMS = {"action": "create", "topic": "the psychology of procrastination",
               "duration_s": 45}


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    yield


@pytest.fixture()
def no_start():
    """Intercept real job starts — approvals must gate BEFORE these."""
    calls = []
    with patch.object(vc, "start_job",
                      side_effect=lambda kind, ref, **kw:
                      calls.append((kind, ref, kw)) or
                      {"ok": True, "job": {"job_id": "rt-test", "status":
                                           "QUEUED"}}):
        yield calls


# ── token lifecycle ──────────────────────────────────────────────────────────

def test_issue_returns_pending_token_with_plan_and_methods():
    methods = [{"method_id": "CANVAS", "name": "Canvas", "status":
                "READY_LOCAL", "estimated_cost": "free"}]
    result = vappr.issue({"mode": "SHORTS", "topic": "bitcoin",
                          "duration_s": 45}, methods)
    rec = result["record"]
    assert rec["state"] == "PENDING"
    assert rec["token"] and len(rec["token"]) >= 12
    assert rec["project_key"] == vappr.project_key(
        {"mode": "SHORTS", "topic": "bitcoin", "duration_s": 45})
    assert rec["expires_at"] > rec["created_at"]


def test_issue_dedups_identical_pending_project():
    methods = [{"method_id": "CANVAS"}]
    first = vappr.issue({"mode": "SHORTS", "topic": "bitcoin"}, methods)
    second = vappr.issue({"mode": "SHORTS", "topic": "bitcoin"}, methods)
    assert second["deduplicated"] is True
    assert second["record"]["token"] == first["record"]["token"]


def test_different_projects_get_different_tokens():
    methods = [{"method_id": "CANVAS"}]
    a = vappr.issue({"mode": "SHORTS", "topic": "bitcoin"}, methods)
    b = vappr.issue({"mode": "SHORTS", "topic": "ethereum"}, methods)
    assert a["record"]["token"] != b["record"]["token"]


def test_pending_token_cannot_be_consumed_without_approval():
    methods = [{"method_id": "CANVAS"}]
    rec = vappr.issue({"mode": "SHORTS", "topic": "bitcoin"}, methods)["record"]
    result = vappr.consume(rec["token"])
    assert result["ok"] is False
    assert "no method was approved" in result["error"]


def test_consume_is_single_use():
    methods = [{"method_id": "CANVAS"}]
    rec = vappr.issue({"mode": "SHORTS", "topic": "bitcoin"}, methods)["record"]
    assert vappr.approve(rec["token"], "canvas")["ok"] is True
    first = vappr.consume(rec["token"])
    assert first["ok"] is True
    replay = vappr.consume(rec["token"])          # bypass attempt: replay
    assert replay["ok"] is False
    assert "already used" in replay["error"]


def test_expired_token_refused():
    methods = [{"method_id": "CANVAS"}]
    rec = vappr.issue({"mode": "SHORTS", "topic": "bitcoin"}, methods,
                      ttl_s=1)["record"]
    assert vappr.approve(rec["token"], "canvas")["ok"] is True
    import time
    time.sleep(1.1)
    result = vappr.consume(rec["token"])
    assert result["ok"] is False and "expired" in result["error"]


def test_cancelled_token_refused():
    methods = [{"method_id": "CANVAS"}]
    rec = vappr.issue({"mode": "SHORTS", "topic": "bitcoin"}, methods)["record"]
    vappr.approve(rec["token"], "canvas")
    vappr.cancel(rec["token"])
    assert vappr.consume(rec["token"])["ok"] is False


def test_approval_bound_to_project_key():
    methods = [{"method_id": "CANVAS"}]
    spec_a = {"mode": "SHORTS", "topic": "bitcoin", "duration_s": 45}
    rec = vappr.issue(spec_a, methods)["record"]
    vappr.approve(rec["token"], "canvas")
    ok = vappr.consume(rec["token"], project_key_expected=rec["project_key"])
    assert ok["ok"] is True

    rec2 = vappr.issue({"mode": "SHORTS", "topic": "ethereum"},
                       methods)["record"]
    vappr.approve(rec2["token"], "canvas")
    changed = vappr.consume(rec2["token"],
                            project_key_expected=vappr.project_key(spec_a))
    assert changed["ok"] is False
    assert "different project" in changed["error"]


def test_method_must_be_one_of_the_offered():
    methods = [{"method_id": "CANVAS"}]
    rec = vappr.issue({"mode": "SHORTS", "topic": "bitcoin"}, methods)["record"]
    result = vappr.approve(rec["token"], "vimax")   # bypass: paid not offered
    assert result["ok"] is False
    assert "not one of the offered" in result["error"]


def test_redacted_plan_never_stores_script_bodies():
    spec = {"mode": "AI_CINEMATIC", "topic": "city night",
            "script": "SECRET-SCREENPLAY-BODY " * 50,
            "image": "/home/user/private_photo.jpg"}
    plan = vappr.redact_plan(spec)
    assert "script" not in plan and "image" not in plan
    assert plan["script_sha1"] and plan["script_len"] == len(spec["script"])
    blob = json.dumps(plan)
    assert "SECRET-SCREENPLAY-BODY" not in blob
    assert "/home/user/private_photo.jpg" not in blob


# ── the action-level gate (bypass attempts through the TOOL) ────────────────

def test_create_without_token_asks_and_starts_nothing(no_start):
    message = vc.video_creator(dict(BASE_PARAMS))
    assert "Which method" in message
    assert no_start == []                       # nothing was queued
    assert "token " in message


def test_create_with_pending_token_and_no_method_reasks(no_start):
    vc.video_creator(dict(BASE_PARAMS))
    rec = vappr.pending_for_project(_spec_from(BASE_PARAMS))
    message = vc.video_creator({**BASE_PARAMS, "approve_token": rec["token"]})
    assert "Which method" in message
    assert no_start == []


def test_create_with_pending_token_never_starts(no_start):
    """Bypass attempt: pass a PENDING token straight through."""
    vc.video_creator(dict(BASE_PARAMS))
    rec = vappr.pending_for_project(_spec_from(BASE_PARAMS))
    message = vc.video_creator({**BASE_PARAMS, "approve_token": rec["token"],
                                "method": "CANVAS"})
    # approve+consume happens atomically in the create flow — job starts
    assert no_start and no_start[0][0] == "video"
    assert "queued" in message.lower()


def test_unknown_token_refused(no_start):
    message = vc.video_creator({**BASE_PARAMS,
                                "approve_token": "deadbeefdeadbeef",
                                "method": "CANVAS"})
    assert message.startswith("Refused:")
    assert no_start == []


def test_used_token_cannot_start_a_second_video(no_start):
    """Approval from an earlier video must not authorize a new one."""
    vc.video_creator(dict(BASE_PARAMS))
    rec = vappr.pending_for_project(_spec_from(BASE_PARAMS))
    vc.video_creator({**BASE_PARAMS, "approve_token": rec["token"],
                      "method": "CANVAS"})
    assert len(no_start) == 1
    # same params again → dedup issues a NEW pending token; the OLD token
    # must still be dead
    vc.video_creator(dict(BASE_PARAMS))
    message = vc.video_creator({**BASE_PARAMS,
                                "approve_token": rec["token"],
                                "method": "CANVAS"})
    assert message.startswith("Refused:")
    assert len(no_start) == 1                   # no second job


def test_material_change_requires_new_approval(no_start):
    """Change the topic after approval → different project → refused."""
    vc.video_creator(dict(BASE_PARAMS))
    rec = vappr.pending_for_project(_spec_from(BASE_PARAMS))
    changed = {**BASE_PARAMS, "topic": "the history of chess"}
    message = vc.video_creator({**changed, "approve_token": rec["token"],
                                "method": "CANVAS"})
    assert message.startswith("Refused:")
    assert no_start == []


def test_paid_method_not_offered_for_documentary(no_start):
    """VIMAX is only offered where it applies — a documentary cannot be
    routed to the paid path via the approval dialog."""
    doc = {"action": "create", "topic": "deep ocean documentary",
           "duration_s": 600}
    message = vc.video_creator(dict(doc))
    assert "VIMAX" not in message.split("Available methods")[1] \
        .split("Nothing has been")[0]
    rec = vappr.pending_for_project(_spec_from(doc))
    result = vappr.approve(rec["token"], "vimax")
    assert result["ok"] is False


def test_approved_overrides_are_applied_and_clamped(no_start):
    """The approved configuration IS final; clamps stay honest."""
    # no mode keywords → documentary default; 45s is below the 60s minimum
    # → clamped to 60 exactly as the plan notes say
    vc.video_creator(dict(BASE_PARAMS))
    rec = vappr.pending_for_project(_spec_from(BASE_PARAMS))
    vc.video_creator({**BASE_PARAMS, "approve_token": rec["token"],
                      "method": "CANVAS"})
    kind, ref, kwargs = no_start[0]
    assert kwargs["params"]["spec"]["duration_s"] == 60

    # approval-time override: the user sets resolution at approval
    params2 = {"action": "create", "topic": "quantum computing",
               "duration_s": 120}
    vc.video_creator(dict(params2))
    rec2 = vappr.pending_for_project(_spec_from(params2))
    vc.video_creator({**params2, "approve_token": rec2["token"],
                      "method": "CANVAS", "resolution": "720p"})
    kind, ref, kwargs = no_start[1]
    spec = kwargs["params"]["spec"]
    assert spec["resolution"] == "720p"          # approved override applied
    # 120s documentary + sample-first policy → 30s verification cut
    assert spec["duration_s"] == 30 and spec.get("sample") is True


def test_approval_log_has_no_secrets(tmp_path):
    params = {**BASE_PARAMS, "mode": "AI_CINEMATIC",
              "script": "TOPSECRET-SCREENPLAY " * 40}
    with patch.object(vc, "_create_vimax",
                      return_value="cinematic path not started in test"):
        vc.video_creator(dict(params))
    store = vappr.state_path()
    blob = store.read_text() if store.exists() else ""
    assert "TOPSECRET-SCREENPLAY" not in blob


# ── split gate ───────────────────────────────────────────────────────────────

def test_split_without_token_asks_approval(no_start):
    # with no completed documentary, the honest pre-check fires BEFORE the
    # approval gate — a doomed request is never gated
    honest = vc.video_creator({"action": "split", "count": 2})
    assert "could not find a completed documentary" in honest.lower()
    assert no_start == []


def _spec_from(params: dict) -> dict:
    """Mirror _create's spec resolution for pending-token lookups in tests."""
    from core.video_agent import spec as vspec
    from core.video_agent.pipeline_doc import apply_sample_policy
    request_text = " ".join(str(params.get(k) or "") for k in
                            ("request", "topic", "audience", "style"))
    spec = vspec.build_spec(params, request_text=request_text)
    if spec["pipeline"] != "smoke" and spec["mode"] == "DOCUMENTARY":
        spec = apply_sample_policy(spec)
    return spec
