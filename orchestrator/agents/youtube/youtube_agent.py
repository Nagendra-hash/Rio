"""
orchestrator/agents/youtube/youtube_agent.py — the pack as one routed agent.

Registered as ``youtube_agent`` on the BaseAgent contract under the same
shadow-mode convention as every other specialist
(``JARVIS_YOUTUBE_AGENT=on``).

ACTIONS (metadata.action, or inferred from the ask)

    run          the full loop: research → script → production → review →
                 [publish] → learn. Nothing is uploaded without approval.
    research     niche + audience + blocked topics → an editorial plan.
    script       hook, sections, CTA, claims, metadata (title/desc/tags) and a
                 thumbnail concept.
    production   narration + visuals + a real assembly plan (honest about
                 "simulated"); render=true tries to produce a real MP4.
    review       readiness + fact ledger + an advisory SEO audit → gates, and a
                 local Review Studio page.
    publish      schedule, and upload ONLY with approved=true AND publish=true.
    learn        capture evidence, measure the KPI and economics, propose the
                 next move (pending until approved).
    approve      approve the pending learnings (they may then steer planning).
    status       what the run has produced so far.
    list         the actions and the guardrails.

`handle()` NEVER raises. Every failure is an AgentResult(FAILED) with the reason
and the artifact folder, never a fake success; a request to fabricate evidence
or engagement is DENIED before any work happens.
"""
from __future__ import annotations

import re
from pathlib import Path

from orchestrator.agents.youtube import agents, policy, schema
from orchestrator.agents.youtube.llm import HouseLLM
from orchestrator.agents.youtube.pipeline import ORDER, YouTubePipeline
from orchestrator.base import BaseAgent
from orchestrator.contracts import (AgentRequest, AgentResult, TaskStatus,
                                    TaskType)

_ACTIONS = ("run", "research", "script", "production", "review", "publish",
            "learn", "approve", "status", "list")
_ACTION_PHRASES = (
    ("approve", r"\b(approve|greenlight|sign off|accept)\b"),
    ("publish", r"\b(publish|upload|post it|schedule)\b"),
    ("review", r"\b(review|quality|fact.?check|readiness|gates?|approve before\b)"),
    ("learn", r"\b(learn|analytics|performance|how did it do|metrics|roi|"
              r"kpi|retention)\b"),
    ("production", r"\b(produce|production|render|narration|voiceover|"
                   r"assemble|mp4|video file)\b"),
    ("script", r"\b(script|hook|write the (video|script)|call to action|"
               r"metadata|title and description)\b"),
    ("research", r"\b(research|topic|ideas?|strategy|editorial|plan|"
                 r"what should i (make|film))\b"),
    ("status", r"\b(status|progress|what .* produced)\b"),
)


def _desktop() -> Path:
    return Path.home() / "Desktop"


def _unique_workspace(name: str) -> Path:
    base = _desktop() / name
    if not base.exists():
        return base
    for i in range(2, 60):
        cand = _desktop() / f"{name}-{i}"
        if not cand.exists():
            return cand
    import uuid
    return _desktop() / f"{name}-{uuid.uuid4().hex[:6]}"


def _infer_action(text: str, meta: dict) -> str:
    explicit = str(meta.get("action") or "").strip().lower()
    if explicit in _ACTIONS:
        return explicit
    low = (text or "").lower()
    if re.search(r"\b(youtube|channel|video|thumbnail|subscribers)\b", low) \
            and re.search(r"\b(create|make|build|run|start|do)\b", low) \
            and not re.search(r"\b(publish|upload|review|learn)\b", low):
        return "run"
    for action, pat in _ACTION_PHRASES:
        if re.search(pat, low):
            return action
    return "run"


class YouTubeAgent(BaseAgent):
    name = "youtube_agent"
    description = ("YouTube automation: research topics, write the script and "
                   "metadata, produce narration and visuals, run quality and "
                   "fact checks, schedule and (only with your approval) publish, "
                   "then learn from the results and propose the next move. Never "
                   "uploads without approval, never publishes a placeholder, "
                   "never invents a metric.")
    task_types = (TaskType.YOUTUBE,)

    def __init__(self, llm=None, tts=None) -> None:
        self._llm = llm if llm is not None else HouseLLM()
        # An injectable narration synthesizer tts(text, out_path) -> bool.
        self._tts = tts

    # ── entry ────────────────────────────────────────────────────────────
    def handle(self, request: AgentRequest) -> AgentResult:
        try:
            return self._handle(request)
        except Exception as exc:                  # never break the router
            return AgentResult(task_id=request.task_id,
                               status=TaskStatus.FAILED,
                               text=f"[youtube_agent] internal error: {exc}",
                               agent=self.name)

    def _handle(self, request: AgentRequest) -> AgentResult:
        meta = dict(request.metadata or {})
        text = request.text or ""
        action = _infer_action(text, meta)

        if action == "list":
            return self._ok(request, self._help(), {"action": "list"})

        # ── guardrail gate ───────────────────────────────────────────────
        bad = policy.assert_no_fabrication(text)
        if not bad.allowed:
            return AgentResult(task_id=request.task_id,
                               status=TaskStatus.DENIED, text=bad.reason,
                               agent=self.name)

        # ── workspace ────────────────────────────────────────────────────
        channel = _channel_from(meta)
        workspace = str(meta.get("workspace") or meta.get("repo_root") or "").strip()
        if not workspace:
            name = (channel.name if channel else "Channel") or "Channel"
            safe = re.sub(r"[^A-Za-z0-9]", "", name) or "Channel"
            workspace = str(_unique_workspace(f"{safe}YouTube"))
        workspace = str(Path(workspace).expanduser())

        steps = meta.get("steps")
        if isinstance(steps, str):
            steps = [s.strip() for s in steps.split(",") if s.strip()]
        single = action if action in ("research", "script", "production",
                                      "review", "publish", "learn") else None
        if single:
            steps = [single]
        elif action == "approve":
            steps = ["learn"]
        elif action == "run":
            steps = list(meta.get("steps") or ORDER)

        render = bool(meta.get("render") or meta.get("build"))
        publish = bool(meta.get("publish"))
        approved = bool(meta.get("approved"))
        if action == "publish" and meta.get("publish") is None:
            publish = bool(meta.get("approved"))     # "publish" action needs both

        pipe = YouTubePipeline(workspace, llm=self._llm, tts=self._tts)
        if channel is not None:
            pipe.project.save_channel(channel)

        if action == "status":
            return self._ok(request, self._render_status(pipe.status()),
                            {"action": "status", **pipe.status()})

        approve = _as_list(meta.get("approve"))
        reject = _as_list(meta.get("reject"))
        if action == "approve" and not approve:
            approve = ["*"]

        result = pipe.run(
            channel=channel, steps=steps, topic=str(meta.get("topic") or ""),
            count=int(meta.get("count") or 3),
            research_signals=_as_list(meta.get("research_signals")),
            source_catalog=_as_list(meta.get("source_catalog")),
            approved_learnings=_as_list(meta.get("approved_learnings")),
            render=render, provider=str(meta.get("provider") or "slideshow"),
            narration_file=str(meta.get("narration_file") or ""),
            approved=approved, publish=publish,
            publish_time=str(meta.get("publish_time") or ""),
            privacy=str(meta.get("privacy") or ""),
            verified_sources=_as_list(meta.get("verified_sources")),
            sources=meta.get("sources") if isinstance(meta.get("sources"),
                                                      list) else None,
            claims=meta.get("claims") if isinstance(meta.get("claims"),
                                                   list) else None,
            waive=_as_list(meta.get("waive")),
            waive_note=str(meta.get("waive_note") or ""),
            evidence=meta.get("evidence") if isinstance(meta.get("evidence"), dict)
            else None,
            retention_curves=meta.get("retention_curves")
            if isinstance(meta.get("retention_curves"), dict) else None,
            approve=approve, reject=reject,
            resume=bool(meta.get("resume", True)))

        status = TaskStatus.COMPLETED if result.get("ok") else TaskStatus.FAILED
        return AgentResult(task_id=request.task_id, status=status,
                           text=self._render(result, action, publish),
                           data=result, agent=self.name)

    # ── rendering ────────────────────────────────────────────────────────
    def _ok(self, request, text, data=None) -> AgentResult:
        return AgentResult(task_id=request.task_id, status=TaskStatus.COMPLETED,
                           text=text, data=data or {}, agent=self.name)

    def _help(self) -> str:
        return (
            "[youtube_agent] YouTube automation, human-in-command.\n"
            "Actions: run, research, script, production, review, publish, "
            "learn, approve, status, list.\n"
            f"Pipeline: {' → '.join(ORDER)}.\n"
            "Tell me the channel (name, audience, pillars, KPI, blocked topics) "
            "and I'll plan, write, produce and review. Publishing needs "
            "approved=true AND publish=true.\n"
            + policy.publishing_banner())

    def _render(self, result: dict, action: str, publish: bool) -> str:
        lines = [f"[youtube_agent] action '{action}' → "
                 f"{'ok' if result.get('ok') else 'not complete'}"
                 f" | ready to publish: {'yes' if result.get('ready') else 'no'}"]
        if result.get("channel_name"):
            lines.append(f"Channel: {result['channel_name']}")
        lines.append(f"Workspace: {result.get('youtube_dir') or result.get('workspace')}")
        stages = result.get("stages") or {}
        for name in ORDER:
            st = stages.get(name)
            if not st:
                continue
            if name == "review":
                mark = "✓" if st.get("ready") else "•"
            elif name == "publish":
                mark = "✓" if st.get("allowed") else "•"
            else:
                mark = "✓" if st.get("ok") else "•"
            lines.append(f"  {mark} {name}: {str(st.get('note', ''))[:170]}")
        review = stages.get("review") or {}
        if review.get("gates"):
            for gate, okk in review["gates"].items():
                lines.append(f"  gate {gate}: {'PASS' if okk else 'FAIL'}")
        publish_stage = stages.get("publish") or {}
        if publish_stage:
            lines.append(f"  publish: {'ALLOWED' if publish_stage.get('allowed') else 'BLOCKED'}"
                         f" — {str(publish_stage.get('reason') or '')[:160]}")
        learn = stages.get("learn") or {}
        if learn.get("recommendations"):
            lines.append(f"  learn: {len(learn['recommendations'])} proposal(s), "
                         f"approved={len(learn.get('approved') or [])}, "
                         f"pending={len(learn.get('pending') or [])}")
        lines.append(f"Artifacts under: {result.get('youtube_dir', '')}")
        if not publish:
            lines.append("Say 'publish it' (with approved=true, publish=true) to "
                         "upload — I never upload without your approval.")
        lines.append(policy.publishing_banner())
        return "\n".join(lines)

    def _render_status(self, status: dict) -> str:
        lines = ["[youtube_agent] status",
                 f"Channel: {status.get('channel_name') or '(none yet)'}",
                 f"Workspace: {status.get('workspace')}"]
        stages = status.get("stages") or {}
        if stages:
            for name, st in stages.items():
                lines.append(f"  {'✓' if st.get('ok') else '•'} {name}: "
                             f"{str(st.get('note', ''))[:140]}")
        else:
            lines.append("  (no run yet — tell me your channel and say 'make a "
                         "video')")
        pending = status.get("pending_recommendations") or []
        if pending:
            lines.append(f"Pending learnings ({len(pending)}): "
                         + "; ".join(str(p.get("title")) for p in pending))
        n = len(status.get("artifacts") or [])
        lines.append(f"Artifacts: {n} file(s) under {status.get('workspace')}/youtube")
        return "\n".join(lines)


def _channel_from(meta: dict) -> schema.Channel | None:
    raw = meta.get("channel")
    if isinstance(raw, schema.Channel):
        return raw.normalised()
    if isinstance(raw, dict):
        return schema.parse_channel(raw)
    return None


def _as_list(value):
    if value is None:
        return None
    if isinstance(value, (list, tuple)):
        return list(value)
    if isinstance(value, str):
        return [v.strip() for v in value.split(",") if v.strip()]
    return [value]
