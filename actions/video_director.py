"""
actions/video_director.py — Phase 5 voice surface: the VideoDirector agent.

'Jarvis, plan a video about …'          → full project plan (storyboard,
                                           scenes, cameras, characters,
                                           estimates, engine tradeoffs)
'Jarvis, direct a 45-second short …'    → same, shorts mode

The director NEVER generates anything — it plans, estimates and recommends.
Generation happens only through video_creator's mandatory method approval
(say 'create it with <method>' afterwards; the plan's scenes feed the
cinematic path automatically when the user picks ViMax).
"""
from __future__ import annotations

from core.video_agent import director


def video_director(parameters: dict, player=None, speak=None) -> str:
    parameters = parameters or {}
    action = str(parameters.get("action") or "plan").strip().lower()
    try:
        if action == "plan":
            return _plan_text(parameters)
        return ("Unknown director action. Use plan — e.g. 'plan a video "
                "about the Mariana Trench'.")
    except ValueError as exc:
        return f"Refused: {exc}."
    except Exception as exc:                        # noqa: BLE001
        return (f"The director is unavailable: {type(exc).__name__}: "
                f"{str(exc)[:180]}. Nothing was generated.")


def _plan_text(parameters: dict) -> str:
    plan = director.plan_video(parameters)
    if not plan.get("ok"):
        return plan.get("question") or "I could not build a plan."
    lines = [
        f"Video plan — “{plan['title']}”",
        f"Logline: {plan['logline']}",
    ]
    if plan.get("style_notes"):
        lines.append(f"Style: {plan['style_notes']}")
    lines.append("")
    lines.append(f"Storyboard ({plan['storyboard_source']}):")
    for scene in plan["scenes"]:
        camera = f" · camera: {scene['camera']}" if scene.get("camera") \
            else ""
        lines.append(f"  {scene['id']}. [{scene['duration_s']}s] "
                     f"{scene['title']}{camera}")
        if scene.get("visual_prompt"):
            lines.append(f"     visual: {scene['visual_prompt'][:140]}")
    if plan["characters"]:
        lines.append("")
        lines.append("Characters:")
        for c in plan["characters"]:
            lines.append(f"  • {c['name']} — {c['description'][:120]}")
    lines.append("")
    lines.append("Engine options (measured status, honest tradeoffs):")
    for rec in plan["render_estimates"]:
        mark = " (recommended)" if rec.get("recommended") else ""
        lines.append(f"  • {rec['name']} [{rec['method_id']}]{mark} — "
                     f"{rec['status']} · {rec['estimate']}")
        lines.append(f"    {rec['tradeoff']}")
    if plan.get("notes"):
        lines.append("")
        lines.append(f"Notes: {'; '.join(plan['notes'])}")
    lines.append("")
    lines.append(plan["next_step"])
    return "\n".join(lines)


TOOL = {
    "name": "video_director",
    "description": (
        "The Video Director: turns 'make a video about X' into a full "
        "PROJECT PLAN before anything is generated — title and logline, a "
        "scene-by-scene storyboard with per-scene generation prompts, "
        "camera moves and motion, a character list, render-time estimates "
        "and engine recommendations with honest quality/speed/cost "
        "tradeoffs. Actions: plan ('plan a video about …', 'direct a "
        "45-second short about …'). It NEVER starts a render — after the "
        "plan, the user picks a method and video_creator's approval flow "
        "starts it ('create it with Canvas / Blender 3D / ViMax')."),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": "plan (the only action today)",
            },
            "topic": {
                "type": "STRING",
                "description": "The video idea/topic.",
            },
            "request": {
                "type": "STRING",
                "description": ("The user's original phrasing (mode/duration "
                                "detection: 'a 45-second Short about …')."),
            },
            "mode": {
                "type": "STRING",
                "description": ("DOCUMENTARY | SHORTS | AI_CINEMATIC | "
                                "BLENDER_3D — auto-detected when omitted."),
            },
            "duration_s": {
                "type": "INTEGER",
                "description": "Target total length in seconds.",
            },
            "style": {
                "type": "STRING",
                "description": "Visual style, e.g. 'dark cinematic'.",
            },
            "language": {
                "type": "STRING",
                "description": "Narration language, e.g. 'Hindi'.",
            },
            "audience": {
                "type": "STRING",
                "description": "Target audience/platform.",
            },
        },
        "required": ["action", "topic"],
    },
    "handler": video_director,
}
