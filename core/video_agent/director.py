"""
core/video_agent/director.py — Phase 5 (Universal Video Studio): the
VideoDirector agent.

Turns a natural-language request into a structured VIDEO PROJECT PLAN:

  * topic + style understanding (spec via the existing build_spec clamps)
  * a working title, logline and style notes
  * a scene-by-scene storyboard: per scene a summary, the generation prompt,
    a CAMERA move, motion notes and a REAL duration that sums to the target
  * a character/asset list (names, descriptions, where they appear)
  * render estimates per candidate engine and an honest
    quality / speed / cost tradeoff
  * engine recommendations (from the measured Phase 2 registry)

HARD RULE: the director NEVER starts generation, never queues a job, never
downloads anything. It produces the plan; the Phase 6 approval flow starts
whatever the user approves.

LLM: one structured call through the house router (script_writer.HouseLLM,
RESEARCH task). If no LLM answers, the plan degrades honestly to a heuristic
scene split labelled "heuristic" — it never invents an LLM-authored
storyboard.
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any

from core.video_agent import spec as vspec
from core.video_agent.engines import registry as engine_registry
from core.video_agent.script_writer import HouseLLM, extract_json

__all__ = ["plan_video", "load_plan", "plans_dir"]

_MIN_SCENE_S = 3


def plans_dir() -> Path:
    from core.repo_agent.jobs import db_path
    return db_path().parent / "video_plans"


def load_plan(plan_id: str) -> dict[str, Any] | None:
    path = plans_dir() / f"plan-{str(plan_id)[:24]}.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


# ── the LLM storyboard call ──────────────────────────────────────────────────

_DIRECTOR_SYSTEM = (
    "You are a video director. You answer with STRICT JSON only — no prose, "
    "no markdown fences. You never invent facts; you plan watchable scenes.")


def _director_prompt(spec: dict[str, Any]) -> str:
    mode = spec["mode"]
    if mode == "DOCUMENTARY":
        craft = ("Documentary style: factual, calm pacing, each scene covers "
                 "one beat of the story with a Ken-Burns-friendly visual.")
    elif mode == "SHORTS":
        craft = ("Vertical short: HOOK in the first 2 seconds, fast cuts, "
                 "caption-friendly scenes, strong payoff at the end.")
    elif mode == "BLENDER_3D":
        craft = ("3D animation: simple expressive characters, one clear "
                 "action per scene, camera moves a real rig can do (push-in, "
                 "orbit, follow, static).")
    else:
        craft = ("Cinematic: consistent visual language across scenes, "
                 "camera and lighting described per shot.")
    return (
        f"Plan a {mode.replace('_', ' ').lower()} video.\n"
        f"Topic: {spec['topic']}\n"
        f"Style: {spec.get('style') or 'director chooses'}\n"
        f"Language: {spec.get('language') or 'simple English'}\n"
        f"Total duration: {spec['duration_s']} seconds\n"
        f"Audience: {spec.get('target_platform') or 'general'}\n\n"
        f"{craft}\n\n"
        "Return EXACTLY this JSON shape:\n"
        '{"title": str, "logline": str, "style_notes": str, '
        '"characters": [{"name": str, "description": str}], '
        '"scenes": [{"id": 1, "title": str, "summary": str, '
        '"visual_prompt": str, "camera": str, "motion": str, '
        '"duration_s": int}]}\n'
        f"Use 3 to 8 scenes for this duration; each scene {max(3, spec['duration_s'] // 8)}"
        f"-{max(8, spec['duration_s'] // 3)} seconds. "
        "Scene visual_prompt describes what a video generator should render. "
        "camera is one of: static, slow push-in, pull-back, orbit, pan left, "
        "pan right, follow, handheld, crane up.")


def _llm_storyboard(spec: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
    llm = HouseLLM()
    raw = llm.text(_director_prompt(spec), system=_DIRECTOR_SYSTEM)
    data = extract_json(raw)
    if not data or not isinstance(data.get("scenes"), list) \
            or not data["scenes"]:
        return None, ""
    return data, getattr(llm, "name", "llm")


# ── heuristic fallback (honest) ──────────────────────────────────────────────

def _heuristic_storyboard(spec: dict[str, Any]) -> dict[str, Any]:
    duration = int(spec["duration_s"])
    count = max(3, min(6, duration // 20 or 3))
    per = max(_MIN_SCENE_S, duration // count)
    beats = ["Establish the subject", "Give the key context",
             "Show the turning point", "Add the supporting detail",
             "Deliver the payoff", "Close with a takeaway"]
    scenes = []
    for i in range(count):
        scenes.append({
            "id": i + 1,
            "title": beats[i % len(beats)],
            "summary": f"{beats[i % len(beats)]}: {spec['topic']}",
            "visual_prompt": f"{spec['topic']} — {beats[i % len(beats)]}",
            "camera": ("slow push-in", "static", "pan right",
                       "orbit", "pull-back", "static")[i % 6],
            "motion": "gentle Ken Burns",
            "duration_s": per,
        })
    return {"title": spec["topic"][:80].title(),
            "logline": f"A {spec['mode'].lower()} about {spec['topic']}",
            "style_notes": str(spec.get("style") or ""),
            "characters": [], "scenes": scenes}


# ── duration honesty: scenes must sum to the target ─────────────────────────

def _rescale_scenes(scenes: list[dict[str, Any]], total_s: int,
                    notes: list[str]) -> list[dict[str, Any]]:
    clean: list[dict[str, Any]] = []
    for raw in scenes[:12]:
        try:
            dur = max(_MIN_SCENE_S, int(float(raw.get("duration_s") or 0)))
        except (TypeError, ValueError):
            dur = _MIN_SCENE_S
        scene = dict(raw)
        scene["duration_s"] = dur
        scene["id"] = len(clean) + 1
        for key in ("title", "summary", "visual_prompt", "camera", "motion"):
            scene[key] = str(raw.get(key) or "")[:400]
        clean.append(scene)
    raw_sum = sum(s["duration_s"] for s in clean)
    if raw_sum != total_s and raw_sum > 0:
        scale = total_s / raw_sum
        for scene in clean:
            scene["duration_s"] = max(_MIN_SCENE_S,
                                      round(scene["duration_s"] * scale))
        notes.append(f"scene durations rescaled to sum to {total_s}s "
                     f"(LLM proposed {raw_sum}s)")
    return clean


# ── estimates + tradeoffs (from the measured registry) ───────────────────────

def _estimate_for(engine_id: str, duration_s: int) -> str:
    if engine_id == "CANVAS":
        return f"~{max(1, round(duration_s / 60 * 4))} min CPU render (free)"
    if engine_id == "BLENDER_3D":
        return (f"~{max(2, round(duration_s / 10 * 8))} min CPU render "
                f"(Blender frames + encode, free)")
    if engine_id == "VIMAX":
        return ("provider-side generation — minutes per shot; paid credits "
                "(billing currently blocked on this machine's keys)")
    if engine_id in ("WAN_2_1", "LTX_VIDEO"):
        return ("local: incompatible on this GPU; remote worker: minutes "
                "per shot once configured")
    if engine_id == "STOCK_FOOTAGE":
        return ("clip search + ffmpeg assembly — minutes on CPU "
                "(provider licensing may apply)")
    return "estimate unavailable"


_TRADEOFFS = {
    "CANVAS": ("quality: clean motion graphics + real narration · "
               "speed: fastest, runs today · cost: free · privacy: local"),
    "BLENDER_3D": ("quality: true 3D animation · speed: slow (frame "
                   "rendering) · cost: free · privacy: local"),
    "VIMAX": ("quality: AI-generated shots · speed: provider-side minutes "
              "per shot · cost: PAID credits (billing blocked today) · "
              "privacy: prompts go to cloud providers"),
    "WAN_2_1": ("quality: open-weight AI video · speed: needs a CUDA GPU · "
                "cost: free weights, compute on your hardware/worker · "
                "privacy: local or your own remote worker"),
    "LTX_VIDEO": ("quality: fast open-weight AI video · speed: needs a "
                  "CUDA GPU · cost: free weights · privacy: local or your "
                  "own remote worker"),
    "STOCK_FOOTAGE": ("quality: real footage · speed: fast · cost: "
                      "provider licensing · privacy: queries to the "
                      "provider, licensed clips"),
}


def _recommendations(spec: dict[str, Any]) -> list[dict[str, Any]]:
    methods = engine_registry.methods_for_spec(spec)
    out = []
    for m in methods:
        mid = m["method_id"]
        out.append({
            "method_id": mid,
            "name": m["name"],
            "status": m["status"],
            "recommended": m.get("recommended", False),
            "estimate": _estimate_for(mid, int(spec["duration_s"])),
            "tradeoff": _TRADEOFFS.get(mid, "see engine details"),
            "cost": m["estimated_cost"],
            "privacy": m["privacy"],
        })
    return out


# ── the public entry ─────────────────────────────────────────────────────────

def plan_video(parameters: dict[str, Any] | None) -> dict[str, Any]:
    """Build the full video project plan. NEVER starts anything."""
    params = dict(parameters or {})
    topic = str(params.get("topic") or "").strip()
    if not topic:
        request_text = str(params.get("request") or "").strip()
        topic = request_text
    if not topic:
        return {"ok": False,
                "question": "What should the video be about? Give me a "
                            "topic and I will direct the plan — script, "
                            "scenes, cameras, characters and engine "
                            "options."}
    params.setdefault("topic", topic)
    request_text = " ".join(str(params.get(k) or "") for k in
                            ("request", "topic", "audience", "style"))
    try:
        spec = vspec.build_spec(params, request_text=request_text)
    except vspec.SpecError as exc:
        return {"ok": False, "question": exc.question}

    notes: list[str] = list(spec.get("notes") or [])
    storyboard, provider = _llm_storyboard(spec)
    source = "llm"
    if storyboard is None:
        storyboard = _heuristic_storyboard(spec)
        provider = "heuristic (no LLM answered — honest fallback)"
        notes.append("storyboard is a heuristic split — no LLM answered; "
                     "it is NOT AI-authored")
    scenes = _rescale_scenes(storyboard.get("scenes") or [],
                             int(spec["duration_s"]), notes)

    plan = {
        "ok": True,
        "plan_id": _plan_id(spec),
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "spec": spec,
        "title": str(storyboard.get("title") or spec["topic"])[:120],
        "logline": str(storyboard.get("logline") or "")[:400],
        "style_notes": str(storyboard.get("style_notes") or "")[:400],
        "characters": [
            {"name": str(c.get("name") or "")[:80],
             "description": str(c.get("description") or "")[:400]}
            for c in (storyboard.get("characters") or [])[:12]
            if isinstance(c, dict) and c.get("name")
        ],
        "scenes": scenes,
        "storyboard_source": provider,
        "render_estimates": _recommendations(spec),
        "notes": notes,
        "next_step": ("Nothing has been generated. Say 'create it with "
                      "<method>' (e.g. 'create it with Canvas' or 'create "
                      "it with Blender 3D') and the approval flow starts "
                      "the chosen engine."),
    }
    _persist(plan)
    return plan


def _plan_id(spec: dict[str, Any]) -> str:
    import hashlib
    blob = json.dumps({"t": spec.get("topic"), "m": spec.get("mode"),
                       "d": spec.get("duration_s"),
                       "s": spec.get("style")}, sort_keys=True, default=str)
    return hashlib.sha1(blob.encode()).hexdigest()[:12]


def _persist(plan: dict[str, Any]) -> None:
    try:
        directory = plans_dir()
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"plan-{plan['plan_id']}.json"
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(plan, indent=1), encoding="utf-8")
        tmp.replace(path)
    except Exception:                              # noqa: BLE001
        pass                                       # best-effort persistence
