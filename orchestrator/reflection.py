"""
orchestrator/reflection.py — GENERATE → CRITIQUE → IMPROVE.

Reflection is a QUALITY-CONTROL loop over generated output, core
orchestration infrastructure like the Plan-and-Execute engine:

    GENERATE  produce a draft (caller-supplied generator, or the LLM layer)
    CRITIQUE  judge the draft: missing information, contradictions, weak
              reasoning, formatting problems — scored, with a verdict
    IMPROVE   produce a better version addressing the critique
    (loop with a hard iteration cap and a token/time budget)

No loops without ceilings: max_rounds and budgets are mandatory policy
inputs (ReflectionPolicy), clamped by hard constants. The loop stops when
the critic approves, the budget is exhausted, or improvement stalls —
never on its own schedule.

Every LLM surface is injectable (LLMBackend protocol, same pattern as the
other orchestrator modules); the deterministic critic below is the offline
floor so the loop works with no model at all.

Secrets never survive: all drafts, critiques, and stored artifacts pass
through mask_secrets() (the coding agent's choke point) — lessons and
critiques are stored redacted.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from orchestrator.agents.coding_tools import mask_secrets

log = logging.getLogger("jarvis.reflection")

# ── hard ceilings (policy is clamped to these) ──────────────────────────────
MAX_ROUNDS = 3                     # generate→critique→improve rounds
MAX_TOTAL_TOKENS = 60_000          # rough token budget across the loop
MAX_WALL_SECONDS = 300.0           # wall-clock budget for the whole loop
MAX_TEXT_CHARS = 32_000            # per-artifact cap (drafts, critiques)
MAX_LESSONS_STORED = 200


def _clip(text: str | None, cap: int = MAX_TEXT_CHARS) -> str:
    if not text:
        return ""
    return text if len(text) <= cap else text[:cap] + "\n… (truncated)"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def rough_tokens(text: str) -> int:
    """Cheap token estimate (~4 chars/token) for budget accounting."""
    return max(1, len(text or "") // 4)


# ── policy ──────────────────────────────────────────────────────────────────
@dataclass
class ReflectionPolicy:
    """Caller-tunable limits, clamped by the hard ceilings above."""
    max_rounds: int = 2
    max_total_tokens: int = MAX_TOTAL_TOKENS
    max_wall_seconds: float = MAX_WALL_SECONDS
    approve_score: float = 7.5        # critic score ≥ this → approved

    def __post_init__(self) -> None:
        self.max_rounds = max(1, min(MAX_ROUNDS, int(self.max_rounds)))
        self.max_total_tokens = max(1, min(MAX_TOTAL_TOKENS,
                                           int(self.max_total_tokens)))
        self.max_wall_seconds = max(1.0, min(MAX_WALL_SECONDS,
                                             float(self.max_wall_seconds)))

    def to_dict(self) -> dict[str, Any]:
        return {"max_rounds": self.max_rounds,
                "max_total_tokens": self.max_total_tokens,
                "max_wall_seconds": self.max_wall_seconds,
                "approve_score": self.approve_score}


# ── critique ────────────────────────────────────────────────────────────────
@dataclass
class Critique:
    """One critic's judgment of a draft."""
    score: float                      # 0..10
    approved: bool
    missing_info: list[str] = field(default_factory=list)
    contradictions: list[str] = field(default_factory=list)
    weak_reasoning: list[str] = field(default_factory=list)
    formatting: list[str] = field(default_factory=list)
    summary: str = ""
    source: str = "deterministic"     # deterministic | llm
    masked_kinds: list[str] = field(default_factory=list)

    def issues(self) -> list[str]:
        out = []
        for kind in ("missing_info", "contradictions", "weak_reasoning",
                     "formatting"):
            for item in getattr(self, kind):
                out.append(f"{kind}: {item}")
        return out

    def to_dict(self) -> dict[str, Any]:
        return {"score": self.score, "approved": self.approved,
                "missing_info": self.missing_info,
                "contradictions": self.contradictions,
                "weak_reasoning": self.weak_reasoning,
                "formatting": self.formatting,
                "summary": _clip(self.summary, 2000),
                "source": self.source}


# ── the LLM layer (injectable, same protocol as planner/plan_executor) ─────
class LLMBackend:
    """Protocol: complete(prompt, *, system="") -> str | None."""
    def complete(self, prompt: str, *, system: str = "") -> Optional[str]:
        raise NotImplementedError


def _extract_json(raw: str) -> dict | None:
    if not raw:
        return None
    raw = raw.strip()
    if "```" in raw:
        for part in raw.split("```"):
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


CRITIC_SYSTEM = (
    "You are the JARVIS critic. You judge a draft for missing information, "
    "internal contradictions, weak or unsupported reasoning, and formatting "
    "problems. You are specific and terse; you never rewrite the draft "
    "yourself; you answer with ONE JSON object only.")

CRITIC_PROMPT = """Critique this draft.

TASK:
{task}

DRAFT:
{draft}

REPLY WITH ONE JSON OBJECT, NOTHING ELSE:
{{
  "score": 0-10,
  "missing_info": ["what the task needs that the draft lacks", "..."],
  "contradictions": ["statement that conflicts with another", "..."],
  "weak_reasoning": ["claim without support / bad inference", "..."],
  "formatting": ["structure, tone, or format problems", "..."],
  "summary": "one-line verdict"
}}

RULES:
- Be concrete; quote the offending fragment in each entry.
- score >= {approve} means the draft is good enough to ship as-is.
- Do not invent problems to seem thorough; an empty list is a valid answer.
"""

IMPROVER_SYSTEM = (
    "You are the JARVIS improver. You rewrite a draft to address specific "
    "criticisms, changing only what the critique demands. You answer with "
    "the improved draft only — no commentary, no fences.")

IMPROVER_PROMPT = """Improve this draft.

TASK:
{task}

CURRENT DRAFT:
{draft}

CRITICISM TO ADDRESS:
{issues}

RULES:
- Fix exactly what the criticism lists; keep everything that works.
- Keep the draft's language and format unless the criticism says otherwise.
- Never include credentials or API keys in the output.
- Answer with the improved draft ONLY.
"""


# ── deterministic critic (offline floor) ────────────────────────────────────
_HEDGES = re.compile(r"\b(?:might|maybe|possibly|perhaps|arguably|"
                     r"it seems|probably)\b", re.IGNORECASE)
_UNCITED_CLAIM = re.compile(r"\b(?:always|never|everyone knows|obviously|"
                            r"clearly|definitely|guaranteed)\b", re.IGNORECASE)
_PLACEHOLDERS = re.compile(r"\b(?:TODO|TBD|FIXME|XXX|lorem ipsum|\\.\\.\\.)\b",
                           re.IGNORECASE)
_HEADINGS = re.compile(r"^#{1,3}\\s|\\n#{1,3}\\s")


class DeterministicCritic:
    """Fully offline critique: catches placeholders, missing structure,
    hedge/absolutism density, and emptiness. Score starts at 8 and loses
    points per finding — honest but coarse."""

    def critique(self, task: str, draft: str) -> Critique:
        draft = draft or ""
        missing: list[str] = []
        contradictions: list[str] = []
        weak: list[str] = []
        formatting: list[str] = []
        stripped = draft.strip()
        if not stripped:
            return Critique(score=0.0, approved=False,
                            missing_info=["draft is empty"],
                            summary="empty draft", source="deterministic")
        if _PLACEHOLDERS.search(stripped):
            missing.append("placeholder text (TODO/TBD/…) left in the draft")
        if len(stripped) < 40:
            weak.append("draft is very short for a substantive task")
        hedges = len(_HEDGES.findall(stripped))
        if hedges >= 3:
            weak.append(f"heavy hedging ({hedges} hedge words) — state "
                        f"things plainly or support them")
        absolutes = _UNCITED_CLAIM.findall(stripped)
        if absolutes:
            weak.append(f"unsupported absolutes: "
                        f"{', '.join(sorted({a.lower() for a in absolutes}))}")
        if task and "?" in task and task.strip().endswith("?") \
                and not stripped:
            missing.append("task asked a question the draft never answers")
        if len(stripped) > 1200 and not _HEADINGS.search(stripped) \
                and "\\n\\n" not in stripped:
            formatting.append("long draft with no paragraphs or headings")
        if stripped != draft:
            formatting.append("leading/trailing whitespace")
        issues = len(missing) + len(contradictions) + len(weak) + \
            len(formatting)
        score = max(0.0, min(10.0, 8.0 - 1.5 * issues))
        summary = ("deterministic critique: "
                   f"{issues} issue(s) found") if issues else \
            "deterministic critique: no issues found"
        return Critique(score=round(score, 1), approved=issues == 0,
                        missing_info=missing, contradictions=contradictions,
                        weak_reasoning=weak, formatting=formatting,
                        summary=summary, source="deterministic")


# ── LLM critic / improver ───────────────────────────────────────────────────
class LLMCritic:
    def __init__(self, llm: LLMBackend) -> None:
        self.llm = llm

    def critique(self, task: str, draft: str) -> Critique:
        prompt = CRITIC_PROMPT.format(task=_clip(task, 2000),
                                      draft=_clip(draft),
                                      approve=7.5)
        raw = self.llm.complete(prompt, system=CRITIC_SYSTEM)
        data = _extract_json(raw or "")
        if not data:
            return DeterministicCritic().critique(task, draft)
        def _lst(key: str) -> list[str]:
            vals = data.get(key)
            if not isinstance(vals, list):
                return []
            return [str(v) for v in vals if str(v).strip()]
        try:
            score = max(0.0, min(10.0, float(data.get("score", 5.0))))
        except (TypeError, ValueError):
            score = 5.0
        masked_summary, kinds = mask_secrets(str(data.get("summary") or ""))
        items: dict[str, list[str]] = {}
        all_kinds = list(kinds)
        for key in ("missing_info", "contradictions", "weak_reasoning",
                    "formatting"):
            masked_items = []
            for v in _lst(key):
                m, k = mask_secrets(str(v)[:500])
                masked_items.append(m)
                all_kinds.extend(k)
            items[key] = masked_items
        return Critique(score=score,
                        approved=score >= 7.5 and not any(
                            items.values()),
                        missing_info=items["missing_info"],
                        contradictions=items["contradictions"],
                        weak_reasoning=items["weak_reasoning"],
                        formatting=items["formatting"],
                        summary=masked_summary, source="llm",
                        masked_kinds=sorted(set(all_kinds)))


class LLMImprover:
    def __init__(self, llm: LLMBackend) -> None:
        self.llm = llm

    def improve(self, task: str, draft: str, critique: Critique) -> str | None:
        issues = "\\n".join(f"- {i}" for i in critique.issues()) or \
            "- general quality pass"
        raw = self.llm.complete(
            IMPROVER_PROMPT.format(task=_clip(task, 2000),
                                   draft=_clip(draft), issues=issues),
            system=IMPROVER_SYSTEM)
        if not raw or not raw.strip():
            return None
        improved = raw.strip()
        if improved == draft.strip():
            return None                       # no actual change → stall
        masked, _ = mask_secrets(improved)
        return masked


# ── result ──────────────────────────────────────────────────────────────────
@dataclass
class ReflectionResult:
    """The full story of one reflection run."""
    task: str
    final_output: str
    approved: bool
    rounds_used: int
    score: float
    critique: Critique | None
    tokens_used: int = 0
    wall_seconds: float = 0.0
    budget_exhausted: str = ""        # "rounds" | "tokens" | "time" | ""
    stalled: bool = False             # improver stopped changing anything
    history: list[dict] = field(default_factory=list)
    source: str = "deterministic"     # critic that had the final word

    def to_dict(self) -> dict[str, Any]:
        return {"task": _clip(self.task, 2000),
                "final_output": _clip(self.final_output),
                "approved": self.approved, "rounds_used": self.rounds_used,
                "score": self.score,
                "critique": self.critique.to_dict() if self.critique else None,
                "tokens_used": self.tokens_used,
                "wall_seconds": round(self.wall_seconds, 2),
                "budget_exhausted": self.budget_exhausted,
                "stalled": self.stalled, "history": self.history,
                "source": self.source}


# ── the reflection loop ─────────────────────────────────────────────────────
class Reflector:
    """GENERATE → CRITIQUE → IMPROVE with hard ceilings. Never raises."""

    def __init__(self, generator: Callable[[str], str] | None = None,
                 critic: Any = None, improver: Any = None,
                 llm: LLMBackend | None = None,
                 policy: ReflectionPolicy | None = None) -> None:
        self.policy = policy or ReflectionPolicy()
        # The LLM layer is OPT-IN (JARVIS_REFLECT_LLM=on) or injected — a
        # default-constructed Reflector runs the deterministic floor and
        # never touches the network.
        if llm is None and _llm_wanted():
            llm = _DefaultLLM()
        if generator is None:
            self.generator = _llm_generator(llm) if llm is not None \
                else _empty_generator
        else:
            self.generator = generator
        self.critic = critic or (LLMCritic(llm) if llm is not None
                                 else DeterministicCritic())
        if improver is None and llm is not None:
            improver = LLMImprover(llm)
        self.improver = improver
        self._clock = time.monotonic

    def reflect(self, task: str, draft: str | None = None,
                lessons: list[str] | None = None) -> ReflectionResult:
        """Run the loop. `draft` skips GENERATE (caller already has output
        to critique — the common Jarvis case). `lessons` are prior lessons
        injected into improvement prompts."""
        started = self._clock()
        task = _clip(str(task or ""), 2000)
        result = ReflectionResult(task=task, final_output="", approved=False,
                                  rounds_used=0, score=0.0, critique=None)
        try:
            return self._reflect(task, draft, lessons or [], result, started)
        except Exception as exc:                    # reflection never breaks a task
            log.exception("reflection loop failed")
            result.budget_exhausted = result.budget_exhausted or "error"
            result.critique = result.critique or Critique(
                score=0.0, approved=False, summary=f"reflection error: {exc}")
            return result

    # ── internals ────────────────────────────────────────────────────────
    def _reflect(self, task: str, draft: str | None, lessons: list[str],
                 result: ReflectionResult, started: float) -> ReflectionResult:
        policy = self.policy
        if draft is None:
            draft = self._generate(task, result)
        draft = _clip(mask_secrets(draft or "")[0])
        if not draft.strip():
            result.critique = Critique(score=0.0, approved=False,
                                       summary="empty draft; nothing to "
                                               "reflect on")
            result.final_output = ""
            return result

        current = draft
        best = current
        best_score = -1.0
        stall_count = 0
        for round_no in range(1, policy.max_rounds + 1):
            result.rounds_used = round_no
            if self._out_of_time(started, policy):
                result.budget_exhausted = "time"
                break
            if result.tokens_used >= policy.max_total_tokens:
                result.budget_exhausted = "tokens"
                break

            critique = self._critique(task, current, result)
            result.critique = critique
            result.score = critique.score
            result.source = critique.source
            result.history.append({
                "round": round_no, "at": _now(),
                "score": critique.score, "approved": critique.approved,
                "issues": critique.issues()[:10],
                "summary": critique.summary[:200],
                "draft_chars": len(current)})
            if critique.approved:
                result.approved = True
                best, best_score = current, critique.score
                break
            if critique.score > best_score:
                best, best_score = current, critique.score

            # IMPROVE — but stop when out of budget or clearly stalled
            if round_no == policy.max_rounds or \
                    result.tokens_used >= policy.max_total_tokens or \
                    self._out_of_time(started, policy):
                result.budget_exhausted = \
                    result.budget_exhausted or \
                    ("rounds" if round_no == policy.max_rounds else
                     "tokens" if result.tokens_used >=
                     policy.max_total_tokens else "time")
                break
            improved = self._improve(task, current, critique, lessons,
                                     result)
            if not improved or improved.strip() == current.strip():
                stall_count += 1
                if stall_count >= 2:
                    result.stalled = True
                    result.budget_exhausted = \
                        result.budget_exhausted or "stall"
                    break
                continue
            stall_count = 0
            current = improved

        if best:
            result.final_output = best
        result.wall_seconds = self._clock() - started
        result.score = max(result.score, best_score) if best_score >= 0 \
            else result.score
        return result

    def _out_of_time(self, started: float, policy: ReflectionPolicy) -> bool:
        return (self._clock() - started) >= policy.max_wall_seconds

    def _generate(self, task: str, result: ReflectionResult) -> str:
        try:
            out = self.generator(task)
        except Exception as exc:
            log.warning("generator failed: %s", exc)
            out = ""
        out = out or ""
        result.tokens_used += rough_tokens(out)
        return out

    def _critique(self, task: str, draft: str,
                  result: ReflectionResult) -> Critique:
        result.tokens_used += rough_tokens(draft)
        critique = self.critic.critique(task, draft)
        result.tokens_used += rough_tokens(str(critique.to_dict()))
        return critique

    def _improve(self, task: str, draft: str, critique: Critique,
                 lessons: list[str], result: ReflectionResult) -> str | None:
        if self.improver is None:
            return None
        lesson_text = "\n".join(f"- {l}" for l in lessons[:5])
        improver_task = task if not lesson_text else \
            task + "\n\nPrior lessons to apply:\n" + lesson_text
        try:
            improved = self.improver.improve(improver_task, draft, critique)
        except Exception as exc:
            log.warning("improver failed: %s", exc)
            return None
        if improved:
            result.tokens_used += rough_tokens(improved)
        return improved


def _llm_wanted() -> bool:
    """LLM backends are opt-in via env, like every phase's shadow mode."""
    return os.environ.get("JARVIS_REFLECT_LLM", "off").strip().lower() == "on"


def _empty_generator(task: str) -> str:
    """Offline floor: no generator configured → honest empty draft."""
    return ""


class _DefaultLLM:
    """Lazy Model-Router-backed LLM (RouterLLM pattern), built on demand."""
    _inner: Any = None

    def complete(self, prompt: str, *, system: str = "") -> Optional[str]:
        cls = type(self)
        if cls._inner is None:
            try:
                from orchestrator.planner import RouterLLM
                cls._inner = RouterLLM()
            except Exception:
                return None
        try:
            return cls._inner.complete(prompt, system=system)
        except Exception:
            return None


def _llm_generator(llm: LLMBackend) -> Callable[[str], str]:
    def generate(task: str) -> str:
        return llm.complete(f"Write the best possible answer to this task. "
                            f"Answer only.\\n\\nTASK:\\n{task}",
                            system="You are the JARVIS generator. You write "
                                   "clear, complete, honest answers.") or ""
    return generate
