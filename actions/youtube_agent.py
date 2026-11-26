"""youtube_agent.py — YouTube automation, human-in-command.

One action over the `orchestrator.agents.youtube` pack, adapted from the
upstream "Lumen" YouTube Automation Agent (MIT): it runs the six-stage loop —
research → script → production → review → publish → learn — on JARVIS.

    youtube_agent(action="run", channel={"name": "My Channel",
                  "pillars": ["Home espresso"], "kpi": "watch_time"})
    youtube_agent(action="script", topic="Dialing in a new grinder")
    youtube_agent(action="production", narrate=true)   # JARVIS TTS → real MP4
    youtube_agent(action="review")
    youtube_agent(action="publish", approved=true, publish=true)  # your call
    youtube_agent(action="learn", evidence={"24h": {...}, "7d": {...}})
    youtube_agent(action="learn", approve=["Double down on this topic"])

Nothing uploads without approved=true AND publish=true. A placeholder render,
missing narration or an unsupported factual claim blocks publishing — and the
agent refuses, before any work, a request to fake statistics or engagement.

Runs in a worker thread. Slow actions (a full run, production/render, publish)
return immediately and announce completion through the UI/speech callback, so
they never hold the Gemini Live tool call open long enough to kill the session.
Quick actions keep the chat-visible timeout.
"""

from __future__ import annotations

import shutil
import threading
import time
from pathlib import Path

_QUICK_TIMEOUT_S = 150  # research/script/review/status without a render
_RUN_LOCK = threading.Lock()  # one slow channel run at a time (state.json)

_ACTIONS = {
    "run",
    "research",
    "script",
    "production",
    "review",
    "publish",
    "learn",
    "approve",
    "status",
    "list",
}


def _as_bool(value) -> bool:
    """Parse a tool argument without the bool("false") == True trap.

    Gemini's function schema says BOOLEAN, but live calls have arrived with
    strings ('true'/'false'). A plain bool() would read 'false' as approval,
    which is unacceptable on the publish gate.
    """
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)


def _agent():
    from orchestrator.agents.youtube.youtube_agent import YouTubeAgent

    return YouTubeAgent(tts=_jarvis_tts)


# ── narration: JARVIS's own TTS, written to a file the pack can mux ──────────
def _jarvis_tts(text: str, out_path: str) -> bool:
    """Synthesize narration with whatever TTS JARVIS has, to a WAV file.

    The pack itself has no TTS dependency; this is the host glue. Order:
    gTTS (JARVIS depends on it) → a system TTS that can write a file. Returns
    True only when a real, non-trivial audio file was written.
    """
    text = " ".join(str(text or "").split())
    if not text:
        return False
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)

    chunks = _chunks(text)
    if _gtts_to_wav(chunks, out):
        return True

    for exe in ("espeak-ng", "espeak"):
        path = shutil.which(exe)
        if path:
            return _run([path, "-w", str(out), text[:4000]]) and _nonempty(out)
    flite = shutil.which("flite")
    if flite:
        return _run([flite, "-t", text, "-o", str(out)]) and _nonempty(out)
    return False


def _chunks(text: str, size: int = 180) -> list[str]:
    """Split narration into TTS-friendly pieces at sentence/space boundaries."""
    import re

    pieces, buf = [], ""
    for part in re.split(r"(?<=[.!?])\s+", text):
        while len(part) > size:
            cut = part.rfind(" ", 0, size) or size
            buf = (buf + " " + part[:cut]).strip()
            if buf:
                pieces.append(buf)
            buf, part = "", part[cut:].lstrip()
        if len(buf) + len(part) + 1 > size and buf:
            pieces.append(buf)
            buf = part
        else:
            buf = (buf + " " + part).strip()
    if buf:
        pieces.append(buf)
    return pieces or [text[:size]]


def _gtts_to_wav(chunks: list[str], out: Path) -> bool:
    try:
        from gtts import gTTS
    except Exception:
        return False
    tmp_dir = out.parent / "_tts"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    mp3s: list[Path] = []
    try:
        for i, chunk in enumerate(chunks):
            mp3 = tmp_dir / f"part{i:03d}.mp3"
            gTTS(text=chunk, lang="en").save(str(mp3))
            if not mp3.is_file() or mp3.stat().st_size < 100:
                return False
            mp3s.append(mp3)
    except Exception:
        return False
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        # No ffmpeg: keep the first mp3 as-is (the pack's muxer needs ffmpeg
        # anyway, so reaching here means there is nothing to assemble).
        shutil.copyfile(mp3s[0], out)
        return _nonempty(out)
    listing = tmp_dir / "list.txt"
    listing.write_text("".join(f"file '{p.name}'\n" for p in mp3s), encoding="utf-8")
    ok = _run(
        [
            ffmpeg,
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(listing),
            "-ar",
            "44100",
            "-ac",
            "1",
            str(out),
        ]
    )
    for p in mp3s:
        p.unlink(missing_ok=True)
    listing.unlink(missing_ok=True)
    return ok and _nonempty(out)


def _run(cmd: list[str]) -> bool:
    import subprocess

    try:
        subprocess.run(cmd, check=True, capture_output=True, timeout=300)
        return True
    except Exception:
        return False


def _nonempty(path: Path) -> bool:
    return path.is_file() and path.stat().st_size > 1000


def youtube_agent(parameters: dict, player=None, speak=None) -> str:
    p = parameters or {}
    action = str(p.get("action") or "").strip().lower()
    channel = p.get("channel") if isinstance(p.get("channel"), dict) else None
    workspace = str(p.get("workspace") or "").strip()
    render = _as_bool(p.get("render")) or _as_bool(p.get("build"))
    narrate = _as_bool(p.get("narrate"))
    narration_file = str(p.get("narration_file") or "").strip()
    if narrate or narration_file:
        render = True
    publish = _as_bool(p.get("publish"))
    approved = _as_bool(p.get("approved"))

    if action and action not in _ACTIONS:
        return (
            f"Unknown action '{action}'. Use one of: " f"{', '.join(sorted(_ACTIONS))}."
        )

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
    for key in (
        "topic",
        "count",
        "publish_time",
        "privacy",
        "provider",
        "steps",
        "approve",
        "reject",
        "verified_sources",
        "narration_file",
        "waive",
        "waive_note",
    ):
        if p.get(key) not in (None, "", []):
            meta[key] = p[key]
    for key in ("sources", "claims"):  # the reviewer's resolved ledger
        if isinstance(p.get(key), list):
            meta[key] = p[key]
    if "resume" in p:
        meta["resume"] = _as_bool(p.get("resume"))
    # New reviewer input (a resolved ledger or a waiver) means "re-review":
    # don't let the resume shortcut skip the stage we were just asked to run.
    if any(
        p.get(k)
        for k in ("sources", "claims", "waive", "waive_note", "verified_sources")
    ):
        meta["resume"] = False
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
        text = (
            f"youtube {action or 'run'} {channel.get('name', '') if channel else ''}"
        ).strip()

    from orchestrator.contracts import AgentRequest, TaskType, new_task_id

    request = AgentRequest(
        task_id=new_task_id("youtube"),
        task_type=TaskType.YOUTUBE,
        text=text,
        metadata=meta,
    )

    outcome: dict = {"done": False, "text": "", "status": "failed"}
    label = action or "run"
    slow = render or label in ("run", "production", "publish")

    def _announce():
        """Background completion: show the artifact and say it is ready.

        Called from the worker thread, never from the Live event loop. Both
        callbacks are optional and guarded — a detached run must not crash on
        a closed UI or a session that reconnected meanwhile.
        """
        body = outcome["text"] or "(no output)"
        status = outcome["status"]
        if player:
            try:
                player.write_log(f"[youtube] {label} → {status}")
            except Exception:
                pass
            try:
                player.show_content(f"YouTube {label}", body)
            except Exception:
                pass
        if not speak:
            return
        if status == "denied":
            msg = "The YouTube request was declined by the safety policy."
        elif status == "failed":
            msg = (
                f"The YouTube {label} did not complete. " f"The details are on screen."
            )
        else:
            msg = (
                f"The YouTube {label} is ready. I've put the result on "
                f"screen; ask me to walk through it."
            )
        try:
            # Let send_tool_response leave before injecting a client turn.
            # A worker that fails instantly would otherwise race the tool
            # response and speak into a turn the model has not closed.
            time.sleep(1.5)
            speak(msg)
        except Exception:
            pass

    def _work():
        try:
            result = _agent().handle(request)
            outcome["status"] = str(getattr(result.status, "value", "failed"))
            outcome["text"] = str(result.text or "")
        except Exception as exc:  # never silent
            outcome["status"] = "failed"
            outcome["text"] = f"{type(exc).__name__}: {exc}"
        finally:
            outcome["done"] = True
            if slow:
                _RUN_LOCK.release()
                _announce()

    if slow and not _RUN_LOCK.acquire(blocking=False):
        return (
            "A YouTube run is already in progress. Ask for status, or "
            "wait for it to finish before starting another."
        )

    try:
        threading.Thread(target=_work, daemon=True, name="youtube-agent").start()
    except Exception:
        if slow:
            _RUN_LOCK.release()
        raise

    if slow:
        # A render/publish can take minutes. Holding the Live tool call open
        # that long starves the websocket keepalive and drops the session, so
        # acknowledge now and deliver the result through _announce().
        if player:
            try:
                player.write_log(f"[youtube] {label} started in background")
            except Exception:
                pass
        return (
            f"Started the YouTube {label} in the background. I will tell "
            f"you when it finishes; artifacts land in the workspace's "
            f"youtube/ folder."
        )

    budget = _QUICK_TIMEOUT_S
    deadline = time.monotonic() + budget
    while not outcome["done"] and time.monotonic() < deadline:
        time.sleep(0.25)

    if not outcome["done"]:
        return (
            f"The YouTube run is still working (exceeded the {budget}s chat "
            f"budget). Its artifacts are under the workspace's youtube/ "
            f"folder; ask for status shortly."
        )
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
        'Tell me your channel, e.g. youtube_agent(action="run", channel={'
        '"name": "My Channel", "audience": "…", "pillars": ["…"], '
        '"kpi": "watch_time", "blocked_topics": ["…"]}).\n'
        "Actions: run, research, script, production (narrate=true for real "
        "narration + a rendered MP4), review, publish (needs approved=true AND "
        "publish=true), learn (evidence=…, approve=[…]), approve, status.\n"
        "To clear a review blocker, pass the reviewed ledger: sources=[{url, "
        'status:"verified"}], claims=[{text, status:"supported", '
        'sourceIds:[…]}] — or waive=[…] with waive_note="…".\n'
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
                "status, or list.",
            },
            "channel": {
                "type": "OBJECT",
                "description": "Channel config: name, objective, audience, "
                "value_proposition, niche, pillars[], "
                "default_format, default_length, voice, cta, "
                "kpi, target_value, target_window_days, "
                "monthly_budget, currency, blocked_topics[], "
                "privacy_status.",
            },
            "topic": {
                "type": "STRING",
                "description": "A specific topic to script (optional).",
            },
            "render": {
                "type": "BOOLEAN",
                "description": "Produce a real MP4 (ffmpeg). Slow. Default "
                "false — plan only.",
            },
            "narrate": {
                "type": "BOOLEAN",
                "description": "Produce real narration with JARVIS's TTS and "
                "render a narrated MP4. Implies render=true.",
            },
            "narration_file": {
                "type": "STRING",
                "description": "Path to your own narration audio to mux "
                "instead of synthesizing one. Implies "
                "render=true.",
            },
            "publish": {
                "type": "BOOLEAN",
                "description": "Actually upload. Requires approved=true too. "
                "Default false.",
            },
            "approved": {
                "type": "BOOLEAN",
                "description": "The user's explicit approval to publish.",
            },
            "evidence": {
                "type": "OBJECT",
                "description": "For action=learn: {'24h': report, '7d': report} "
                "with real analytics (views, watchTime, "
                "engagement, outcomes).",
            },
            "approve": {
                "type": "ARRAY",
                "items": {"type": "STRING"},
                "description": "Titles of pending learnings to approve, or "
                "['*'] for all.",
            },
            "sources": {
                "type": "ARRAY",
                "items": {"type": "OBJECT"},
                "description": "For review: the fact ledger's sources, each "
                "{id, url, title, status: verified|pending|"
                "rejected}. A claim is only supported by a "
                "source marked verified.",
            },
            "claims": {
                "type": "ARRAY",
                "items": {"type": "OBJECT"},
                "description": "For review: the resolved claims, each {text, "
                "riskLevel, status: supported|waived, "
                "sourceIds[] or sourceUrls[]}. A 'supported' "
                "claim must cite a verified source; a 'waived' "
                "claim needs reviewer notes.",
            },
            "waive": {
                "type": "ARRAY",
                "items": {"type": "STRING"},
                "description": "For review: claim texts (or substrings, or "
                "['*']) the reviewer waives. Requires "
                "waive_note.",
            },
            "waive_note": {
                "type": "STRING",
                "description": "The reviewer's reason for waiving — required "
                "for a waiver to count.",
            },
            "resume": {
                "type": "BOOLEAN",
                "description": "Skip stages already done (default true). "
                "Passing sources/claims/waive re-runs review "
                "automatically.",
            },
            "workspace": {
                "type": "STRING",
                "description": "Optional folder to run in (defaults to "
                "~/Desktop/<Channel>YouTube).",
            },
        },
        "required": [],
    },
    "handler": youtube_agent,
}
