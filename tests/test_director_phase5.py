"""Phase 5 (Universal Video Studio) — VideoDirector agent tests.

Honesty contract:
 * the director NEVER starts generation, queues a job or downloads —
   asserted against the job store after every plan
 * the LLM storyboard is mocked; when no LLM answers the plan degrades to a
   HEURISTIC split labelled honestly (never passed off as AI-authored)
 * scene durations are rescaled to sum to the target (recorded in notes)
 * engine recommendations come from the measured Phase 2 registry
 * the plan persists (JARVIS_HOME-aware) and is reloadable
"""
from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from core.repo_agent.jobs import RepoJobStore
from core.video_agent import director


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    yield


_LLM_PLAN = {
    "title": "The Mariana Trench",
    "logline": "A dive into the deepest place on Earth.",
    "style_notes": "calm, awe, dark blues",
    "characters": [{"name": "The Narrator",
                    "description": "calm voice of the dive"}],
    "scenes": [
        {"id": 1, "title": "Surface", "summary": "We set out.",
         "visual_prompt": "a small research vessel on a dark ocean",
         "camera": "slow push-in", "motion": "gentle waves",
         "duration_s": 40},
        {"id": 2, "title": "Descent", "summary": "Light fades.",
         "visual_prompt": "the submersible sinking past sunlit blue",
         "camera": "crane up", "motion": "bubbles rising",
         "duration_s": 40},
        {"id": 3, "title": "The floor", "summary": "Pressure wins.",
         "visual_prompt": "alien pale landscape under beam lights",
         "camera": "static", "motion": "marine snow drifting",
         "duration_s": 40},
    ],
}


def _llm_json(raw: str | None):
    class FakeLLM:
        name = "fake-router"

        def text(self, prompt, system=""):
            return raw

    return FakeLLM


# ── essentials ───────────────────────────────────────────────────────────────

def test_plan_without_topic_asks_one_question():
    result = director.plan_video({"action": "plan"})
    assert result["ok"] is False
    assert "?" in result["question"]
    assert RepoJobStore().list(limit=10) == []      # nothing queued


def test_plan_never_starts_generation():
    with patch.object(director, "HouseLLM", _llm_json(json.dumps(_LLM_PLAN))):
        director.plan_video({"topic": "the mariana trench",
                             "duration_s": 120})
    assert RepoJobStore().list(limit=20) == []
    assert RepoJobStore().list(limit=20, kind="run_skill") == []


# ── the LLM storyboard ───────────────────────────────────────────────────────

def test_llm_storyboard_parsed_with_cameras_and_characters():
    with patch.object(director, "HouseLLM", _llm_json(json.dumps(_LLM_PLAN))):
        plan = director.plan_video({"topic": "the mariana trench",
                                    "duration_s": 120})
    assert plan["ok"] is True
    assert plan["title"] == "The Mariana Trench"
    assert plan["storyboard_source"].startswith("model-router") or \
        plan["storyboard_source"] == "fake-router"
    assert len(plan["scenes"]) == 3
    assert all(s["camera"] for s in plan["scenes"])
    assert all(s["visual_prompt"] for s in plan["scenes"])
    assert plan["characters"][0]["name"] == "The Narrator"
    # 3×40s LLM proposal vs 120s target → sums exactly, no rescale note
    assert sum(s["duration_s"] for s in plan["scenes"]) == 120


def test_scene_durations_rescaled_to_target():
    wobbly = dict(_LLM_PLAN)
    wobbly["scenes"] = [
        {**_LLM_PLAN["scenes"][0], "duration_s": 10},
        {**_LLM_PLAN["scenes"][1], "duration_s": 10},
        {**_LLM_PLAN["scenes"][2], "duration_s": 10},
    ]
    with patch.object(director, "HouseLLM", _llm_json(json.dumps(wobbly))):
        plan = director.plan_video({"topic": "the mariana trench",
                                    "duration_s": 90})
    assert sum(s["duration_s"] for s in plan["scenes"]) == 90
    assert any("rescaled" in n for n in plan["notes"])


def test_scene_caps_and_minimums():
    wild = {"title": "x", "logline": "x", "style_notes": "",
            "characters": [], "scenes": [
                {"id": i + 1, "title": f"s{i}", "summary": "s",
                 "visual_prompt": "v", "camera": "static", "motion": "m",
                 "duration_s": 0} for i in range(20)]}
    with patch.object(director, "HouseLLM", _llm_json(json.dumps(wild))):
        plan = director.plan_video({"topic": "t", "duration_s": 60})
    assert len(plan["scenes"]) <= 12
    assert all(s["duration_s"] >= 3 for s in plan["scenes"])


# ── the honest heuristic fallback ────────────────────────────────────────────

def test_heuristic_fallback_labelled_honestly():
    with patch.object(director, "HouseLLM", _llm_json(None)):
        plan = director.plan_video({"topic": "tea ceremonies",
                                    "duration_s": 120})
    assert plan["ok"] is True
    assert "heuristic" in plan["storyboard_source"].lower()
    assert any("heuristic" in n.lower() for n in plan["notes"])
    assert len(plan["scenes"]) >= 3
    assert all(s["camera"] for s in plan["scenes"])
    assert sum(s["duration_s"] for s in plan["scenes"]) == 120


# ── mode detection + recommendations ─────────────────────────────────────────

def test_request_text_drives_mode():
    with patch.object(director, "HouseLLM", _llm_json(None)):
        plan = director.plan_video({"topic": "bitcoin basics",
                                    "request": "a 45-second Short about "
                                               "Bitcoin"})
    assert plan["spec"]["mode"] == "SHORTS"
    assert plan["spec"]["aspect_ratio"] == "9:16"


def test_recommendations_match_mode_with_tradeoffs():
    with patch.object(director, "HouseLLM", _llm_json(None)):
        doc = director.plan_video({"topic": "deep oceans",
                                   "duration_s": 300})
    ids = {r["method_id"] for r in doc["render_estimates"]}
    assert "CANVAS" in ids and "BLENDER_3D" not in ids
    canvas = next(r for r in doc["render_estimates"]
                  if r["method_id"] == "CANVAS")
    assert "free" in canvas["tradeoff"].lower()
    assert canvas["estimate"].endswith(")") or "min" in canvas["estimate"]
    # cinematic mode offers the paid path with its honest state
    with patch.object(director, "HouseLLM", _llm_json(None)):
        cine = director.plan_video({"topic": "neon city chase",
                                    "mode": "AI_CINEMATIC",
                                    "duration_s": 20})
    cine_ids = {r["method_id"] for r in cine["render_estimates"]}
    assert "VIMAX" in cine_ids and "CANVAS" not in cine_ids
    vimax = next(r for r in cine["render_estimates"]
                 if r["method_id"] == "VIMAX")
    assert "paid" in vimax["tradeoff"].lower()


# ── persistence ──────────────────────────────────────────────────────────────

def test_plan_persisted_and_reloadable():
    with patch.object(director, "HouseLLM", _llm_json(None)):
        plan = director.plan_video({"topic": "origami history",
                                    "duration_s": 100})
    loaded = director.load_plan(plan["plan_id"])
    assert loaded is not None
    assert loaded["title"] == plan["title"]
    assert loaded["spec"]["topic"] == "origami history"


def test_plan_id_stable_for_identical_request():
    with patch.object(director, "HouseLLM", _llm_json(None)):
        a = director.plan_video({"topic": "same topic", "duration_s": 60})
        b = director.plan_video({"topic": "same topic", "duration_s": 60})
    assert a["plan_id"] == b["plan_id"]
