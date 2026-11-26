"""
orchestrator/agents/study/safety.py — the Phase 15 study guardrails.

Two jobs, both in code (the finance-agent / productivity pattern):

1. THE HOMEWORK REFUSAL — live exams, graded assignments, and someone
   else's homework are refused with a fixed, honest message while any
   legitimate learning ask is allowed. The agent organizes, explains and
   drills — it does not sit an exam for you.

2. PERMISSION LEVELS — same vocabulary as productivity/security:

    READ_ONLY           study status, progress, plan view, explain, quiz
                        (nothing persisted except well-being checkpoints).
    SAFE_ACTION         everything the study agent persists is about the
                        USER'S OWN learning: goals, plans, quiz results,
                        progress, box ladders. Still local-only and
                        redacted, so creating/updating them is SAFE, not
                        approval-gated (the dailylife convention: local
                        writes to the user's own data never need approval).
    BLOCKED             exam-cheating, impersonating a student, disabling
                        the refusal — never runs, even approved.

3. REDACTION reuse — every reply and every store write passes
   productivity.security.redact before leaving this package.
"""
from __future__ import annotations

import re

from orchestrator.agents.productivity.security import (  # noqa: F401  (re-export)
    APPROVAL_REQUIRED, BLOCKED, READ_ONLY, SAFE_ACTION, redact, redact_obj)

CHEAT_REFUSAL = (
    "I'm a study partner, not a homework machine — I can't take exams or "
    "complete graded assignments on your behalf. What I CAN do: explain the "
    "topic, build a study plan, make practice questions, and track your "
    "progress so the exam gets easier for YOU.")

# Phrases that ask the agent to BE the student on a live, graded thing.
_CHEAT_RE = re.compile(
    r"\b(?:take\s+(?:the\s+|my\s+)?(?:exam|test|quiz)\s+for\s+me|"
    r"sit\s+(?:the\s+|my\s+)?(?:exam|test)\s+for\s+me|"
    r"answ(?:er|ers)\s+(?:the\s+|my\s+)?(?:live|ongoing|graded|current)\s+"
    r"(?:exam|test|quiz|assignment)|"
    r"(?:live|ongoing|graded|current)\s+(?:exam|test|quiz|assignment)"
    r".{0,60}?(?:answ(?:er|ers)?|do|finish|complete)|"
    r"(?:exam|test|quiz|assignment).{0,20}\bis\s+(?:live|happening\s+now"
    r"|ongoing|right\s+now)|"
    r"do\s+my\s+(?:home\s?work|assignment|coursework)\s+for\s+me|"
    r"write\s+my\s+(?:exam|essay\s+for\s+the\s+exam))\b",
    re.IGNORECASE)


def is_cheat_request(text: str) -> bool:
    """True when the ask is to take/complete a live graded assessment."""
    return bool(_CHEAT_RE.search(str(text or "")))


def permission_for(action: str) -> str:
    """The level IS the answer — there is no try-and-see."""
    if action in ("status", "progress", "goal_view", "plan_view",
                  "explain", "resources", "quiz", "grade"):
        return READ_ONLY
    if action in ("goal_set", "plan", "quiz_result", "reset",
                  "difficulty", "prefs"):
        return SAFE_ACTION
    if action in ("take_exam_for_user", "do_homework", "impersonate"):
        return BLOCKED
    return READ_ONLY
