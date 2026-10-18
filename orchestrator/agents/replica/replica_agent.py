"""
orchestrator/agents/replica/replica_agent.py — the pack as one routed agent.

Registered as `replica_agent` on the BaseAgent contract under the SAME
shadow-mode convention as every other specialist
(``JARVIS_REPLICA_AGENT=on``). It sits *under the coding agent* in spirit:
the builder step hands its plan to the existing Android coding pipeline
(`actions/dev_agent`, which shares `orchestrator.agents.android_support`), so
the two surfaces cannot drift.

ACTIONS (metadata.action, or inferred from the ask)

    clone     the full pack: recon → architecture → design → critic →
              enhance → [build] → verify. A Play Store URL in the ask is the
              app. Clean-room rules apply.
    recon     reverse-engineer the public listing into a recon map.
    plan      architecture + feature matrix + the app spec (the "study" step).
    design    tokens + component specs + a contrast check.
    critic    rank the real reviews in replica/reviews.csv.
    enhance   fold the fixes in, name/rebrand, write the store listing.
    build     hand the spec to the coding agent and build the APK.
    verify    parity + rebrand sweep + listing lint gates.
    status    what the run has produced so far.
    list      the actions and the clean-room rules.

Clean-room guardrails are enforced: a request that asks to decompile the app,
or a source URL that serves the APK, is DECLINED before any work happens.

`handle()` NEVER raises. Every failure is an AgentResult(FAILED) with the
reason and the artifact folder, never a fake success.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

from orchestrator.agents.replica import agents, policy
from orchestrator.agents.replica.llm import HouseLLM
from orchestrator.agents.replica.pipeline import ORDER, ReplicaPipeline
from orchestrator.base import BaseAgent
from orchestrator.contracts import (AgentRequest, AgentResult, TaskStatus,
                                    TaskType)

PLAY_URL_RE = re.compile(
    r"https?://play\.google\.com/store/apps/details\?[^\s\"']+", re.I)

_ACTIONS = ("clone", "recon", "plan", "design", "critic", "enhance", "build",
            "verify", "status", "list")
_ACTION_PHRASES = (
    ("verify", r"\b(verify|check|parity|is it ready|gates?)\b"),
    ("build", r"\b(build|apk|compile|assemble)\b"),
    ("enhance", r"\b(enhance|improve|make it better|brand|name it|position)\b"),
    ("critic", r"\b(reviews?|what .* (hate|complain|want)|feedback)\b"),
    ("design", r"\b(design|tokens?|palette|theme|look and feel)\b"),
    ("plan", r"\b(plan|architecture|study|schema|spec)\b"),
    ("recon", r"\b(recon|reverse.?engineer|map|screens|analyse|analyze|study the app)\b"),
    ("status", r"\b(status|progress|what .* produced)\b"),
)


def _desktop() -> Path:
    return Path.home() / "Desktop"


def _unique_workspace(name: str) -> Path:
    base = _desktop() / f"{name}"
    if not base.exists():
        return base
    for i in range(2, 60):
        cand = _desktop() / f"{name}-{i}"
        if not cand.exists():
            return cand
    import uuid
    return _desktop() / f"{name}-{uuid.uuid4().hex[:6]}"


def _app_name_from_url(url: str) -> str:
    m = re.search(r"[?&]id=([A-Za-z0-9_.]+)", url or "")
    if m:
        leaf = m.group(1).split(".")[-1]
        return re.sub(r"[^A-Za-z0-9]", " ", leaf).title().replace(" ", "") or "Replica"
    return "Replica"


def _infer_action(text: str, meta: dict) -> str:
    explicit = str(meta.get("action") or "").strip().lower()
    if explicit in _ACTIONS:
        return explicit
    low = (text or "").lower()
    if re.search(r"\bclone\b|\bcopy\b|rebuild", low) and PLAY_URL_RE.search(text or ""):
        return "clone"
    for action, pat in _ACTION_PHRASES:
        if re.search(pat, low):
            return action
    if PLAY_URL_RE.search(text or ""):
        return "clone"
    return "clone"


class ReplicaAgent(BaseAgent):
    name = "replica_agent"
    description = ("Clean-room app rebuilds from a Google Play listing: recon "
                   "the app, plan an Android build, read its reviews to find "
                   "what to improve, rebuild it as your own app, then verify "
                   "parity and rebrand before it ships. Never decompiles the "
                   "original or copies its code, assets or branding.")
    task_types = (TaskType.REPLICA,)

    def __init__(self, llm=None) -> None:
        self._llm = llm if llm is not None else HouseLLM()

    # ── entry ────────────────────────────────────────────────────────────
    def handle(self, request: AgentRequest) -> AgentResult:
        try:
            return self._handle(request)
        except Exception as exc:                  # never break the router
            return AgentResult(task_id=request.task_id,
                               status=TaskStatus.FAILED,
                               text=f"[replica_agent] internal error: {exc}",
                               agent=self.name)

    def _handle(self, request: AgentRequest) -> AgentResult:
        meta = dict(request.metadata or {})
        text = request.text or ""
        action = _infer_action(text, meta)

        if action == "list":
            return self._ok(request, self._help(), {"action": "list"})

        # ── clean-room gate ──────────────────────────────────────────────
        bad = policy.assert_no_decompile(text)
        if not bad.allowed:
            return AgentResult(task_id=request.task_id,
                               status=TaskStatus.DENIED, text=bad.reason,
                               agent=self.name)

        url = str(meta.get("listing_url") or "").strip()
        if not url:
            m = PLAY_URL_RE.search(text)
            url = m.group(0) if m else ""

        # ── workspace ────────────────────────────────────────────────────
        app_name = str(meta.get("app_name") or "").strip()
        workspace = str(meta.get("workspace") or meta.get("repo_root") or "").strip()
        if not workspace:
            name = app_name or _app_name_from_url(url or text)
            workspace = str(_unique_workspace(f"{name}Replica"))
        workspace = str(Path(workspace).expanduser())

        steps = meta.get("steps")
        if isinstance(steps, str):
            steps = [s.strip() for s in steps.split(",") if s.strip()]
        build = action == "build" or action == "clone" and bool(meta.get("build"))
        if action in ("recon", "plan", "design", "critic", "enhance", "verify"):
            build = False
        fail_under = float(meta.get("fail_under") or 80.0)

        pipe = ReplicaPipeline(workspace, llm=self._llm)

        if action == "status":
            return self._ok(request, self._render_status(pipe.status()),
                            {"action": "status", **pipe.status()})

        listing = meta.get("listing") if isinstance(meta.get("listing"), dict) \
            else None
        single = None
        if action in ("recon", "plan", "design", "critic", "enhance"):
            single = {"recon": "recon", "plan": "architecture", "design": "design",
                      "critic": "critic", "enhance": "enhance"}[action]

        result = pipe.run(listing_url=url, listing=listing, app_name=app_name,
                          steps=[single] if single else None,
                          build=build, resume=bool(meta.get("resume", True)),
                          fail_under=fail_under,
                          project_dir=str(meta.get("project_dir") or ""))

        status = TaskStatus.COMPLETED if result.get("ok") else TaskStatus.FAILED
        return AgentResult(task_id=request.task_id, status=status,
                           text=self._render(result, action, url, build),
                           data=result, agent=self.name)

    # ── rendering ────────────────────────────────────────────────────────
    def _ok(self, request, text, data=None) -> AgentResult:
        return AgentResult(task_id=request.task_id, status=TaskStatus.COMPLETED,
                           text=text, data=data or {}, agent=self.name)

    def _help(self) -> str:
        return (
            "[replica_agent] Clean-room app rebuilds from a Play listing.\n"
            "Actions: clone, recon, plan, design, critic, enhance, build, "
            "verify, status, list.\n"
            f"Pipeline: {' → '.join(ORDER)}.\n"
            + policy.clean_room_banner() + "\n"
            "It reads the PUBLIC listing and the app's observable behaviour "
            "only. It never decompiles the APK, never copies the original's "
            "code, strings, artwork or branding, and refuses a request that "
            "asks it to.")

    def _render(self, result: dict, action: str, url: str, build: bool) -> str:
        lines = [f"[replica_agent] action '{action}' "
                 f"→ {'ok' if result.get('ok') else 'not complete'}"]
        if result.get("app_name"):
            lines.append(f"App: {result['app_name']}")
        if url:
            lines.append(f"Listing: {url}")
        lines.append(f"Workspace: {result.get('replica_dir') or result.get('workspace')}")
        for name in ORDER:
            st = (result.get("stages") or {}).get(name)
            if not st:
                continue
            mark = "✓" if st.get("ok") else "•"
            lines.append(f"  {mark} {name}: {st.get('note', '')[:160]}")
        for gate, okk in (result.get("stages", {}).get("verify", {})
                          .get("gates", {}) or {}).items():
            lines.append(f"  gate {gate}: {'PASS' if okk else 'FAIL'}")
        ideas = (result.get("stages", {}).get("enhance", {}) or {}).get("names")
        if ideas:
            lines.append("Name ideas: " + ", ".join(ideas[:5]))
        lines.append("Artifacts under: " + result.get("replica_dir", ""))
        if not build and action in ("clone",):
            lines.append("Say 'build it' (action=build) to compile the APK.")
        lines.append(policy.clean_room_banner())
        return "\n".join(lines)

    def _render_status(self, status: dict) -> str:
        lines = ["[replica_agent] status",
                 f"Workspace: {status.get('workspace')}",
                 f"App: {status.get('app_name') or '(none yet)'}"]
        stages = status.get("stages") or {}
        if stages:
            for name, st in stages.items():
                lines.append(f"  {'✓' if st.get('ok') else '•'} {name}: "
                             f"{st.get('note', '')[:140]}")
        else:
            lines.append("  (no run yet — say 'clone <play store link>')")
        n = len(status.get("artifacts") or [])
        lines.append(f"Artifacts: {n} file(s) under {status.get('workspace')}/replica")
        return "\n".join(lines)
