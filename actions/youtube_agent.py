"""youtube_agent.py — YouTube automation, human-in-command.

One action over the `orchestrator.agents.youtube` pack, adapted from the
upstream "Lumen" YouTube Automation Agent (MIT): it runs the six-stage loop —
research → script → production → review → publish → learn — on JARVIS.

    youtube_agent(action="run", channel={"name": "My Channel",
                  "pillars": ["Home espresso"], "kpi": "watch_time"})
    youtube_agent(action="script", topic="Dialing in a new grinder")
    youtube_agent(action="production", render=true)          # try a real MP4
    youtube_agent(action="review")
    youtube_agent(action="publish", approved=true, publish=true)  # your call
    youtube_agent(action="learn", evidence={"24h": {...}, "7d": {...}})
    youtube_agent(action="learn", approve=["Double down on this topic"])

Nothing uploads without approved=true AND publish=true. A placeholder render,
missing narration or an unsupported factual claim blocks publishing — and the
agent refuses, before any work, a request to fake statistics or engagement.

Runs in a worker thread with a chat-visible timeout, like run_agent: the full
loop with a render can take minutes, so a timeout reports honestly.
"""
from __future__ import annotations

import threading
import time

_TIMEOUT_S = 1500          # a full run with a real render is slow
_QUICK_TIMEOUT_S = 150    # research/script/review/status without a render

_ACTIONS = {"run", "research", "script", "production", "review", "publish",
            "learn", "approve", "status", "list"}


def _agent():
    from orchestrator.agents.youtube.youtube_agent import YouTubeAgent
    return YouTubeAgent()


def youtube_agent(parameters: dict, player=None) -> str:
    p = parameters or {}
    action = str(p.get("action") or "").strip().lower()
    channel = p.get("channel") if isinstance(p.get("channel"), dict) else None
    workspace = str(p.get("workspace") or "").strip()
    render = bool(p.get("render") or p.get("build"))
    publish = bool(p.get("publish"))
    approved = bool(p.get("approved"))

    if action and action not in _ACTIONS:
        return (f"Unknown action '{action}'. Use one of: "
                f"{', '.join(sorted(_ACTIONS))}.")

    meta: dict = {}
    if action:
        meta["action"] = action
    if channel:
        meta["channel"] = channel
    if workspace:
        meta["workspace"] = workspace
    if render:
        meta["render"] = True
    if publish:
        meta["publish"] = True
    if approved:
        meta["approved"] = True
    for key in ("topic", "count", "publish_time", "privacy", "provider",
                "steps", "approve", "reject", "verified_sources"):
        if p.get(key) not in (None, "", []):
            meta[key] = p[key]
    if isinstance(p.get("evidence"), dict):
        meta["evidence"] = p["evidence"]
    if isinstance(p.get("retention_curves"), dict):
        meta["retention_curves"] = p["retention_curves"]
    if isinstance(p.get("source_catalog"), list):
        meta["source_catalog"] = p["source_catalog"]
    if isinstance(p.get("research_signals"), list):
        meta["research_signals"] = p["research_signals"]

    if not action and not channel:
        return _help()

    text = str(p.get("request") or "")
    if not text:
        text = (f"youtube {action or 'run'} {channel.get('name', '') if channel else ''}"
                ).strip()

    from orchestrator.contracts import AgentRequest, TaskType, new_task_id
    request = AgentRequest(task_id=new_task_id("youtube"),
                           task_type=TaskType.YOUTUBE, text=text,
                           metadata=meta)

    outcome: dict = {"done": False, "text": "", "status": "failed"}

    def _work():
        try:
            result = _agent().handle(request)
            outcome["status"] = str(getattr(result.status, "value", "failed"))
            outcome["text"] = str(result.text or "")
        except Exception as exc:                    # never silent
            outcome["status"] = "failed"
            outcome["text"] = f"{type(exc).__name__}: {exc}"
        finally:
            outcome["done"] = True

    slow = render or action in ("run", "production", "publish")
    budget = _TIMEOUT_S if slow else _QUICK_TIMEOUT_S
    threading.Thread(target=_work, daemon=True, name="youtube-agent").start()
    deadline = time.monotonic() + budget
    while not outcome["done"] and time.monotonic() < deadline:
        time.sleep(0.25)

    if not outcome["done"]:
        return (f"The YouTube run is still working (exceeded the {budget}s chat "
                f"budget). Its artifacts are under the workspace's youtube/ "
                f"folder; ask for status shortly.")
    body = outcome["text"] or "(no output)"
    if outcome["status"] == "denied":
        return f"[youtube declined] {body}"
    if outcome["status"] == "failed":
        return f"[youtube incomplete] {body}"
    return body


def _help() -> str:
    return (
        "I run a YouTube channel loop for you: research → script → production "
        "→ review → publish → learn.\n"
        "Tell me your channel, e.g. youtube_agent(action=\"run\", channel={"
        "\"name\": \"My Channel\", \"audience\": \"…\", \"pillars\": [\"…\"], "
        "\"kpi\": \"watch_time\", \"blocked_topics\": [\"…\"]}).\n"
        "Actions: run, research, script, production (render=true for a real "
        "MP4), review, publish (needs approved=true AND publish=true), learn "
        "(evidence=…, approve=[…]), approve, status.\n"
        "You keep control: I never upload without your approval, never publish "
        "a placeholder or an unsupported claim, and never invent a metric."
    )


# ── Tool declaration (auto-discovered by core/action_loader.py) ──────────────
TOOL = {
    "name": "youtube_agent",
    "description": (
        "YouTube automation: research topics and build a content plan, write "
        "the script, hook, CTA and metadata (title/description/tags/chapters), "
        "produce narration and visuals and assemble a real MP4, run quality and "
        "fact checks (Review Studio), schedule and (only with explicit "
        "approval) publish, then capture 24h/7d evidence and propose the next "
        "move. Use when the user says 'make a YouTube video', 'plan my channel', "
        "'write a video script', 'grow my channel', 'upload this to YouTube', "
        "or asks about subscribers/watch-time/retention. Never uploads without "
        "approved=true AND publish=true; never publishes a placeholder or an "
        "unsupported claim; never invents a metric or buys engagement."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": "run (default full loop), research, script, "
                               "production, review, publish, learn, approve, "
                               "status, or list."
            },
            "channel": {
                "type": "OBJECT",
                "description": "Channel config: name, objective, audience, "
                               "value_proposition, niche, pillars[], "
                               "default_format, default_length, voice, cta, "
                               "kpi, target_value, target_window_days, "
                               "monthly_budget, currency, blocked_topics[], "
                               "privacy_status."
            },
            "topic": {
                "type": "STRING",
                "description": "A specific topic to script (optional)."
            },
            "render": {
                "type": "BOOLEAN",
                "description": "Try to produce a real MP4 (ffmpeg + system "
                               "TTS). Slow. Default false — plan only."
            },
            "publish": {
                "type": "BOOLEAN",
                "description": "Actually upload. Requires approved=true too. "
                               "Default false."
            },
            "approved": {
                "type": "BOOLEAN",
                "description": "The user's explicit approval to publish."
            },
            "evidence": {
                "type": "OBJECT",
                "description": "For action=learn: {'24h': report, '7d': report} "
                               "with real analytics (views, watchTime, "
                               "engagement, outcomes)."
            },
            "approve": {
                "type": "ARRAY",
                "items": {"type": "STRING"},
                "description": "Titles of pending learnings to approve, or "
                               "['*'] for all."
            },
            "workspace": {
                "type": "STRING",
                "description": "Optional folder to run in (defaults to "
                               "~/Desktop/<Channel>YouTube)."
            }
        },
        "required": []
    },
    "handler": youtube_agent,
}
