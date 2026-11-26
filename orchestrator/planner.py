"""
orchestrator/planner.py — turns a goal into an inspectable Plan.

Two planners, one contract (planner.make_plan(goal, ...) -> Plan | None):

  * LLMPlanner  — one Model-Router completion (ModelTask.LIGHT_REASONING)
    returning strict JSON matching the plan schema. The LLM never runs
    anything; it only proposes steps that the validator + executor then
    police. Malformed JSON, unknown actions, or dependency cycles fall back
    to the heuristic planner (and say so in plan.source).

  * HeuristicPlanner — fully offline, deterministic. Splits a multi-sentence
    / numbered / "and then" goal into ordered steps; single-sentence goals
    become one agent step. Used as the always-available floor: a plan exists
    even when no model is configured or the model misbehaves.

Neither planner executes anything. A produced plan is a *proposal* that must
pass Plan.validate() and — for steps flagged requires_approval — a human
gate before the executor touches it.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Optional

from orchestrator.plans import (
    ACTIONS,
    MAX_STEPS,
    Plan,
    PlanStep,
    PlanValidationError,
    make_plan,
)

log = logging.getLogger("jarvis.planner")

# ── the LLM layer (same injectable pattern as the other agents) ─────────────
class LLMBackend:
    """Protocol: complete(prompt, *, system="") -> str | None."""
    def complete(self, prompt: str, *, system: str = "") -> Optional[str]:
        raise NotImplementedError


class RouterLLM(LLMBackend):
    """Model Router first (cheapest-capable), core.gemini's ladder behind it.
    Neither raising nor retrying here; both layers already do that internally."""

    def __init__(self) -> None:
        self._router: Any = None

    def complete(self, prompt: str, *, system: str = "") -> Optional[str]:
        try:
            from orchestrator.model_router import (ModelRouter, ModelTask,
                                                   RouteOptions)
            if self._router is None:
                self._router = ModelRouter()
            res = self._router.route(
                prompt, RouteOptions(task=ModelTask.LIGHT_REASONING,
                                     system=system, timeout_s=60,
                                     deadline_s=90))
            if res.ok and res.text:
                return res.text
        except Exception:
            pass
        try:
            from core import gemini
            text = gemini.text(f"{system}\n\n{prompt}".strip(),
                               tier=gemini.SMART, timeout_ms=60_000)
            return text or None
        except Exception:
            return None


# ── prompts ──────────────────────────────────────────────────────────────────
PLANNER_SYSTEM = (
    "You are the JARVIS planner. You break a goal into a SHORT sequence of "
    "small, concrete, verifiable steps. You never claim to run anything, "
    "never invent actions outside the allowed list, never put credentials "
    "in parameters, and you answer with ONE JSON object only.")

PLANNER_PROMPT = """Break this goal into steps.

GOAL:
{goal}

AVAILABLE CONTEXT (may be empty):
{context}

REPLY WITH ONE JSON OBJECT, NOTHING ELSE:
{{
  "understanding": "what is really being asked (1-2 sentences)",
  "steps": [
    {{"id": "s1", "title": "one line", "action": "agent", "params": {{"task_type": "research", "text": "..." }},
      "depends_on": [], "verification": "how we know it worked",
      "on_fail": "abort", "requires_approval": false, "max_retries": 1}}
  ],
  "risk_notes": ["one line per risk"]
}}

ALLOWED ACTIONS (nothing else):
- "agent": delegate to a JARVIS capability. params: {{"task_type": "coding|research|trading|rag|system", "text": "<the sub-task>"}}
- "llm": one model completion that needs no tools. params: {{"prompt": "..."}}
- "shell": a read-only, whitelisted command (ls, cat, ps, df, du, uname, whoami, date, uptime, git status, git log, git diff, pip list). params: {{"command": "<single command, no ; | && >"}}
- "noop": placeholder / marker step. params: {{}}

RULES:
- {max_steps} steps maximum; fewer is better. Each step = one small outcome.
- Use "depends_on" when a step needs an earlier step's result (step ids s1, s2, ...).
- "requires_approval": true for anything destructive, financial, or hard to undo.
- on_fail: "abort" (default) stops the plan; "skip" drops this step but keeps going; "continue" runs dependent steps anyway.
- If the goal is vague, still produce your best literal interpretation as steps.
"""


def _extract_json(raw: str) -> dict | None:
    """Tolerant JSON extraction (fences/prose tolerated)."""
    if not raw:
        return None
    raw = raw.strip()
    if "```" in raw:                       # strip markdown fences
        parts = raw.split("```")
        for part in parts:
            p = part.strip()
            if p.startswith("json"):
                p = p[4:]
            if p.lstrip().startswith("{"):
                raw = p
                break
    if "{" in raw and "}" in raw:
        raw = raw[raw.find("{"): raw.rfind("}") + 1]
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else None
    except Exception:
        return None


# ── heuristic planner (offline floor) ────────────────────────────────────────
_SPLIT_RE = re.compile(
    r"(?:^|\.\s+|\n+|;\s+|\band then\b|\bthen\b|\bafter that\b|\bnext,\s*)",
    re.IGNORECASE)
_NUMBERED_RE = re.compile(r"^\s*(?:\d+[.)]|[a-z][.)]|[-*•])\s+", re.MULTILINE)


class HeuristicPlanner:
    """Deterministic, dependency-free planning. Never configures a model."""

    _IMperative = re.compile(
        r"^(?:fix|write|make|build|create|find|research|check|get|update|"
        r"add|remove|set|run|test|plan|compare|summar\w*|list|show|draft|"
        r"explain|organize|organise)\b", re.IGNORECASE)

    def plan(self, goal: str, context: list[tuple[str, str]] | None = None,
             created_by: str = "heuristic") -> Plan | None:
        goal = (goal or "").strip()
        if not goal:
            return None
        parts = self._split(goal)
        steps: list[PlanStep] = []
        prev_id = ""
        for i, chunk in enumerate(parts[:MAX_STEPS]):
            sid = f"s{i + 1}"
            steps.append(PlanStep(
                id=sid,
                title=chunk[:80],
                action="agent",
                params={"task_type": "research" if self._looks_like_research(chunk) else "coding",
                        "text": chunk},
                depends_on=[prev_id] if prev_id else [],
                verification="sub-result returned by the delegated capability",
                on_fail="abort",
                requires_approval=False,
                max_retries=1,
                created_by=created_by,
            ))
            prev_id = sid
        notes = ["heuristic plan: steps are literal sub-tasks, not model-refined"]
        if len(parts) > MAX_STEPS:
            notes.append(f"goal had {len(parts)} parts; only the first "
                         f"{MAX_STEPS} became steps")
        return make_plan(goal, steps, source="heuristic",
                         created_by=created_by, risk_notes=notes)

    # ── internals ────────────────────────────────────────────────────────
    @staticmethod
    def _split(goal: str) -> list[str]:
        # numbered / bulleted lists win when present
        if _NUMBERED_RE.search(goal):
            chunks = [c.strip() for c in _NUMBERED_RE.split(goal) if c.strip()]
            if len(chunks) >= 2:
                return chunks
        parts = [p.strip(" .;\n") for p in _SPLIT_RE.split(goal) if p and p.strip(" .;\n")]
        if len(parts) >= 2:
            return parts
        return [goal]

    @staticmethod
    def _looks_like_research(chunk: str) -> bool:
        return bool(re.search(
            r"\b(?:search|find out|news|latest|compare|what|who|when|where|"
            r"why|price|weather|research)\b", chunk, re.IGNORECASE))


# ── LLM planner ──────────────────────────────────────────────────────────────
class LLMPlanner:
    """One completion → validated Plan. Falls back to HeuristicPlanner."""

    def __init__(self, llm: LLMBackend | None = None,
                 heuristic: HeuristicPlanner | None = None,
                 allow_shell: bool = False) -> None:
        self._llm = llm or RouterLLM()
        self._heuristic = heuristic or HeuristicPlanner()
        self.allow_shell = allow_shell   # shell stays off unless explicitly on

    def plan(self, goal: str, context: list[tuple[str, str]] | None = None,
             created_by: str = "llm") -> Plan | None:
        goal = (goal or "").strip()
        if not goal:
            return None
        ctx_text = "\n".join(f"- {role}: {text[:200]}" for role, text
                             in (context or [])[-6:])
        prompt = PLANNER_PROMPT.format(goal=goal[:3000],
                                       context=ctx_text or "(none)",
                                       max_steps=MAX_STEPS)
        raw = self._llm.complete(prompt, system=PLANNER_SYSTEM)
        data = _extract_json(raw or "")
        plan = self._plan_from_json(goal, data, created_by=created_by)
        if plan is not None:
            return plan
        log.info("LLM planner unusable (no model / bad JSON / invalid plan); "
                 "falling back to heuristic")
        return self._heuristic.plan(goal, context,
                                    created_by=f"{created_by}+heuristic")

    # ── internals ────────────────────────────────────────────────────────
    def _plan_from_json(self, goal: str, data: dict | None,
                        created_by: str) -> Plan | None:
        if not data:
            return None
        raw_steps = data.get("steps")
        if not isinstance(raw_steps, list) or not raw_steps:
            return None
        steps: list[PlanStep] = []
        for i, item in enumerate(raw_steps[:MAX_STEPS]):
            if not isinstance(item, dict):
                continue
            action = str(item.get("action") or "").strip().lower()
            if action == "shell" and not self.allow_shell:
                # whitelist refused → the step cannot exist in this plan
                log.info("planner dropped shell step (not allowed): %s",
                         str(item.get("title") or "")[:60])
                continue
            params = item.get("params") if isinstance(item.get("params"), dict) else {}
            if action == "shell" and isinstance(params, dict):
                params = dict(params)
                params["command"] = _sanitize_shell_command(
                    str(params.get("command") or ""))
                if not params["command"]:
                    continue
            try:
                steps.append(PlanStep(
                    id=str(item.get("id") or f"s{len(steps) + 1}").strip(),
                    title=str(item.get("title") or "").strip() or f"step {i + 1}",
                    action=action,
                    params=params,
                    depends_on=[str(d) for d in (item.get("depends_on") or [])],
                    verification=str(item.get("verification") or "").strip(),
                    on_fail=str(item.get("on_fail") or "abort").strip().lower(),
                    requires_approval=bool(item.get("requires_approval", False)),
                    max_retries=item.get("max_retries", 1) if
                    isinstance(item.get("max_retries"), int) else 1,
                    created_by=created_by,
                ))
            except PlanValidationError:
                continue
        if not steps:
            return None
        try:
            risks = [str(r) for r in (data.get("risk_notes") or [])
                     if str(r).strip()]
            return make_plan(goal, steps, source="llm",
                             created_by=created_by, risk_notes=risks)
        except PlanValidationError as exc:
            log.info("LLM plan invalid: %s", exc)
            return None


_SHELL_ALLOWLIST = re.compile(
    r"^(?:ls|cat|head|tail|wc|ps|df|du|uname|whoami|date|uptime|hostname|"
    r"git status|git log|git diff|pip list|env|printenv)$"
)
_SHELL_FORBIDDEN = re.compile(r"[;|&`$><]|\b(?:sudo|rm|mv|cp|chmod|chown|"
                              r"kill|curl|wget|ssh|git\s+push|git\s+reset)\b")


def _sanitize_shell_command(command: str) -> str:
    """Keep only single whitelisted read-only commands; empty means refused."""
    command = " ".join((command or "").split())
    if not command or _SHELL_FORBIDDEN.search(command):
        return ""
    head = command.split(" ", 1)[0].strip()
    if head in ("git", "pip"):
        return command if _SHELL_ALLOWLIST.match(command) else ""
    return command if _SHELL_ALLOWLIST.match(head) else ""


# ── module-level convenience (the one call site other code should use) ──────
_default_planner: LLMPlanner | None = None


def make_goal_plan(goal: str, context: list[tuple[str, str]] | None = None,
                   *, use_llm: bool = True) -> Plan | None:
    """Plan a goal. LLM when available/allowed, heuristic floor otherwise."""
    global _default_planner
    if not use_llm:
        return HeuristicPlanner().plan(goal, context)
    if _default_planner is None:
        _default_planner = LLMPlanner()
    try:
        return _default_planner.plan(goal, context)
    except Exception as exc:                 # planning must never crash intake
        log.warning("planner crashed (%s); heuristic floor", exc)
        return HeuristicPlanner().plan(goal, context)
