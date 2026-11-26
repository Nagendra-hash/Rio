"""
orchestrator/agents/study/study_agent.py — the real Study Partner (Phase
15), on the BaseAgent contract under the canonical name `study_agent`,
enabled with JARVIS_STUDY_AGENT=on (shadow-mode).

CAPABILITIES, each with what it guarantees:

    goal set           'learn linear algebra' → a stored goal (subject,
                       level) replaces the previous active goal; needs
                       input when the subject can't be extracted.
    resources          local library (RAG as caller study_agent) +
                       permitted web; honest 'not found/unavailable'
                       labels; never a fabricated link.
    plan               a deterministic dated plan (/engine.plan_for_goal)
                       expanding the goal to its deadline, with revision
                       days; text + stored plan row + checkpoint.
    explain            explanation ONLY from retrieved library evidence —
                       extractive floor (verbatim sentences with [n]
                       citations); ungrounded answers honestly say the
                       library is thin.
    quiz               multiple-choice questions built from cited library
                       passages; nothing generated from thin air.
    grade              grade answers ⇒ a quiz_result row (score, pass,
                       per-question), then ladder move per question-cited
                       box, then the checkpoint.
    progress           goal + plan % + quiz history + ladder + streak +
                       the adapt decision, in one status text.
    adapt              the next_level decision: promote/hold/demote with
                       the mastery reason, level persisted on the goal.

`handle()` NEVER raises. Every mutating action (goal, plan, grade, adapted
level) snapshots a crash-safe checkpoint after the write.
"""
from __future__ import annotations

import json
import logging
from datetime import date

from orchestrator.agents.study import engine, learning, safety
from orchestrator.agents.study.profile import StudyStore
from orchestrator.agents.study.resources import StudyResources
from orchestrator.base import BaseAgent
from orchestrator.contracts import (AgentRequest, AgentResult, TaskStatus,
                                    TaskType)
from orchestrator.agents.productivity.checkpoint import ProductivityCheckpoint, \
    MAX_CHECKPOINTS
from orchestrator.agents.productivity.store import (
    PERMS_DIR, PERMS_FILE)

log = logging.getLogger("jarvis.study")


class StudyCheckpoint(ProductivityCheckpoint):
    """Study-owned checkpoints — same crash-safe contract (tmp+replace,
    redaction, prune, 0600/0700), domain prefix `study-`."""

    def __init__(self, directory=None) -> None:
        from orchestrator.agents.study.profile import study_dir
        super().__init__(directory or (study_dir() / "checkpoints"))

    def save(self, state: dict):
        try:
            import datetime as _dt
            import uuid as _uuid
            stamp = _dt.datetime.now(_dt.timezone.utc) \
                .strftime("%Y%m%dT%H%M%S")
            path = self.dir / f"study-{stamp}-{_uuid.uuid4().hex[:4]}.json"
            if self._write(path, state):
                self._write(self.dir / "latest.json", state)
                self._prune()
                return path
            return None
        except Exception:
            return None

    def _prune(self) -> None:
        try:
            snaps = sorted(p for p in self.dir.glob("study-*.json"))
            for old in snaps[:-MAX_CHECKPOINTS]:
                old.unlink(missing_ok=True)
        except Exception:
            pass

    def list_ids(self) -> list[str]:
        try:
            return sorted(p.stem for p in self.dir.glob("study-*.json"))
        except Exception:
            return []


class StudyAgent(BaseAgent):
    name = "study_agent"
    description = (
        "Study partner: learning goals, study plans, explanations from "
        "your own study library (RAG), practice quizzes, progress "
        "tracking, adaptive difficulty. Refuses to take exams or do "
        "grades for you.")
    task_types = (TaskType.STUDY,)

    def __init__(self, store: StudyStore | None = None,
                 checkpoint: StudyCheckpoint | None = None,
                 resources: StudyResources | None = None) -> None:
        self.store = store or StudyStore()
        self.checkpoint = checkpoint or StudyCheckpoint()
        self.resources = resources or StudyResources()

    # ── entry point ─────────────────────────────────────────────────────
    def handle(self, request: AgentRequest) -> AgentResult:
        try:
            if safety.is_cheat_request(request.text):
                self.store.audit("cheat_refused")
                return AgentResult(
                    task_id=request.task_id, status=TaskStatus.DECLINED,
                    text=safety.CHEAT_REFUSAL,
                    data={"study": True, "action": "cheat_refused",
                          "refusal": True}, agent=self.name)
            action = str((request.metadata or {}).get("study_action") or "") \
                .strip().lower() or self._intent(request.text)
            handler = self._HANDLERS.get(action, StudyAgent._progress)
            return handler(self, request)
        except Exception as exc:                     # never raise outward
            log.warning("study agent error: %s", type(exc).__name__)
            return AgentResult(task_id=request.task_id,
                               status=TaskStatus.FAILED,
                               error="study action failed", agent=self.name)

    @staticmethod
    def _intent(text: str) -> str:
        t = (text or "").lower()
        if re_ := _re(r"\b(?:what should i (?:study|revise|do next)|"
                      r"what'?s next|now what)\b", t):
            return "adapt"
        if _re(r"\b(?:progress|status|how am i doing)\b", t):
            return "progress"
        if _re(r"\b(?:adapt|adjust|harder|easier|difficulty|level)\b", t):
            return "adapt"
        if _re(r"\b(?:quiz|test me|practice questions)\b", t):
            return "quiz"
        if _re(r"\b(?:answers?|grade|check my|mark)\b", t):
            return "grade"
        if _re(r"\b(?:explain|how does|what is|homework help|help me "
                r"understand|teach me)\b", t):
            return "explain"
        if _re(r"\bresources?\b", t):
            return "resources"
        if _re(r"\b(?:plan|schedule)\b", t):
            return "plan"
        if _re(r"\b(?:goal|set goal|new goal)\b", t):
            return "goal_set"
        if _re(r"\bcomplete[d]?\b|\b(done|finished)\b", t):
            return "plan_done"
        if _re(r"\b(?:learn|study|prepare|revise)\b", t):
            return "plan"
        return "progress"

    # ── helpers ─────────────────────────────────────────────────────────
    def _reply(self, request, text, *, action,
               status=TaskStatus.COMPLETED, extra=None) -> AgentResult:
        return AgentResult(
            task_id=request.task_id, status=status,
            text=safety.redact(text),
            data={"study": True, "action": action, **(extra or {})},
            agent=self.name)

    def _snapshot(self, kind: str) -> None:
        try:
            self.checkpoint.save({"kind": kind,
                                  "state": self.store.summary()})
        except Exception:
            pass

    def _goal(self, request):
        meta = request.metadata or {}
        goal_id = str(meta.get("goal_id") or "").strip()
        if goal_id:
            hit = self.store.get_goal(goal_id)
            if hit:
                return hit
            return None
        active = [g for g in self.store.goals() if not g.get("archived")]
        if meta.get("primary"):
            return self.store.add_goal(str(meta.get("subject") or ""),
                                       level=str(meta.get("level")
                                                   or "beginner"),
                                       deadline=str(meta.get("deadline")
                                                    or ""))
        return active[0] if active else None

    # ── actions ─────────────────────────────────────────────────────────
    def _goal_set(self, request) -> AgentResult:
        meta = request.metadata or {}
        parsed = learning.parse_goal(request.text)
        subject = str(meta.get("subject") or parsed["subject"] or "").strip()
        level = str(meta.get("level") or parsed["level"] or "").lower() \
            or "beginner"
        deadline = str(meta.get("deadline") or "")[:10]
        if not subject:
            return self._reply(
                request, "What should we set as the learning goal? e.g. "
                "'learn linear algebra' (or metadata {subject, level, "
                "deadline}).", action="goal_set",
                status=TaskStatus.NEEDS_INPUT)
        row = self.store.add_goal(subject, level=level, deadline=deadline)
        self._snapshot("goal")
        return self._reply(
            request,
            f"Learning goal set: {row['subject']} [{row['level']}]"
            + (f" (deadline {row['deadline']})" if row['deadline'] else "")
            + ".\nNext: 'study plan' or 'resources' — I'll search your "
              "study library and the web for material.",
            action="goal_set", extra={"goal": row})

    def _resources(self, request) -> AgentResult:
        meta = request.metadata or {}
        goal = self._goal(request)
        topic = str(meta.get("topic") or "").strip() or \
            (goal or {}).get("subject", "")
        if not topic:
            return self._reply(
                request, "Which subject? (text or metadata {topic}/"
                         "{goal_id} after setting a goal)",
                action="resources", status=TaskStatus.NEEDS_INPUT)
        found = self.resources.find(topic, (goal or {}).get("level")
                                    or "beginner")
        lines = [f"Resources — {topic}:"]
        if found.get("kb"):
            lines.append("  your library:")
            for h in found["kb"][:4]:
                lines.append(f"   • {h['title']} ({h['source']})")
        else:
            lines.append(f"  your library: nothing matched"
                         + (f" — {found.get('kb_note', '')}" if
                            found.get("kb_note") else
                            " (empty collection?)" if not found.get("kb_error")
                            else " (search failed)"))
        if found.get("web"):
            lines.append("  found on the web (queried just now):")
            for h in found["web"][:4]:
                lines.append(f"   • {h.get('title') or '(untitled)'} — "
                             f"{h.get('url') or '(no url)'}")
        else:
            lines.append("  web: " + (found.get("web_error")
                                      or "no permitted results — "
                                         "use your library"))
        self.store.audit("resources", have_ranges=False)
        return self._reply(request, "\n".join(lines), action="resources",
                           extra={"kb": found.get("kb"),
                                  "web": found.get("web")})

    def _plan(self, request) -> AgentResult:
        meta = request.metadata or {}
        goal = self._goal(request)
        if not goal:
            parsed = learning.parse_goal(request.text)
            if not parsed["subject"] or _verbish(request.text) is None:
                return self._reply(
                    request, "Set a goal first — 'learn linear "
                             "algebra' — then 'study plan' gives you a "
                             "dated schedule.",
                    action="plan", status=TaskStatus.NEEDS_INPUT)
            goal = self.store.add_goal(parsed["subject"],
                                       level=parsed["level"])
        saved = self.store.latest_plan(goal.get("id", "")) \
            if not meta.get("rebuild") else None
        if saved:
            return self._reply(request, engine.format_plan(saved, goal),
                               action="plan",
                               extra={"plan_id": saved.get("id")})
        plan = learning.plan_for_goal(goal.get("subject", ""),
                                      goal.get("level", "beginner"),
                                      goal.get("deadline", ""))
        plan["note"] = ("teach → drill with quiz days interleaved; last "
                        "3 days reserved for revision, ends with a final "
                        "check. 'complete <session id>' marks a session "
                        "done.")
        row = self.store.save_plan(goal.get("id", ""), plan)
        self._snapshot("plan")
        return self._reply(request, engine.format_plan(row, goal),
                           action="plan", extra={"plan_id": row.get("id"),
                                                 "goal_id": goal.get("id")})

    def _plan_done(self, request) -> AgentResult:
        text = (request.text or "").strip()
        m = _re(r"\b(?:complete[d]?|done|finished)\b\s*(.*)$", text.lower())
        ref = (str((request.metadata or {}).get("session") or "")
               or (m.group(1) if m else "")).strip()
        if not ref:
            return self._reply(
                request, "Which session? 'complete s2' or metadata "
                         "{session: 's2'}.", action="plan_done",
                status=TaskStatus.NEEDS_INPUT)
        hit = self.store.complete_plan_item(ref)
        if not hit:
            return self._reply(
                request, f"No open session {ref!r} in the current plan.",
                action="plan_done")
        self._snapshot("plan")
        return self._reply(
            request, f"Session {hit['id']} done ({hit['topic']}). "
                     "Progress refreshed — say 'progress' to see it.",
            action="plan_done", extra={"session": hit})

    def _explain(self, request) -> AgentResult:
        meta = request.metadata or {}
        goal = self._goal(request)
        topic = str(meta.get("topic") or "").strip() or \
            _after(request.text, r"\b(?:explain|teach me|what is|how "
                                 r"does|help me understand)\b")
        if not topic:
            topic = (goal or {}).get("subject", "") or ""
        if not topic:
            return self._reply(
                request, "What should I explain? (text or metadata "
                         "{topic})", action="explain",
                status=TaskStatus.NEEDS_INPUT)
        level = str(meta.get("level") or (goal or {}).get("level")
                    or "beginner").lower()
        ev = self.resources.explain_evidence(topic)
        if not ev.get("ok"):
            return self._reply(
                request, "Your study library isn't reachable right now: "
                         f"{ev.get('error')}. I won't make up an "
                         "explanation without citations.",
                action="explain")
        expl = engine.extractive_explanation(topic, ev.get("evidence", ""),
                                             level, ev.get("citations", []))
        self.store.audit("explain", grounded=expl.get("ok"))
        body = expl["text"] + (f"\n({expl['level_note']})"
                               if expl.get("level_note") else "")
        return self._reply(
            request, body + "\n\nWant a quiz on this? say 'quiz'." if
            expl.get("ok") else body, action="explain",
            extra={"grounded": expl.get("ok"),
                   "citations": expl.get("citations")})

    def _quiz(self, request) -> AgentResult:
        meta = request.metadata or {}
        goal = self._goal(request)
        topic = str(meta.get("topic") or "").strip() or \
            (goal or {}).get("subject", "")
        n = _int(meta.get("n"), 5)
        if not topic:
            return self._reply(
                request, "What topic should the quiz cover? (metadata "
                         "{topic} or a stored goal)", action="quiz",
                status=TaskStatus.NEEDS_INPUT)
        ev = self.resources.explain_evidence(topic)
        if not ev.get("ok"):
            return self._reply(
                request, "Library unavailable: "
                         f"{ev.get('error')} — no grounded quiz without "
                         "it.", action="quiz")
        quiz = engine.build_quiz(topic,
                                 str(meta.get("level")
                                     or (goal or {}).get("level")
                                     or "beginner"),
                                 ev.get("evidence", ""),
                                 ev.get("citations", []), n=n)
        if not quiz.get("ok"):
            return self._reply(request, quiz.get("error", "no quiz"),
                               action="quiz")
        self._snapshot("quiz")
        self.store.audit("quiz_built", n=len(quiz.get("questions", [])))
        return self._reply(request, engine.render_quiz(quiz),
                           action="quiz",
                           extra={"quiz": quiz, "topic": quiz.get("topic")})

    def _grade(self, request) -> AgentResult:
        meta = request.metadata or {}
        answers = meta.get("answers") or {}
        if not isinstance(answers, dict) or not answers:
            inline = _letters(request.text)
            if inline:
                answers = {f"q{i + 1}": a for i, a in enumerate(inline)}
        quiz = meta.get("quiz") or self._last_quiz_in_context(request)
        if not quiz or not (quiz.get("questions")):
            return self._reply(
                request, "There's no quiz loaded. Ask for a 'quiz' first, "
                         "or pass metadata {quiz, answers}.",
                action="grade", status=TaskStatus.NEEDS_INPUT)
        grade = engine.grade_quiz(quiz, answers or {})
        goal = self._goal(request)
        correct_blocks = {q["cite_block"] for q in grade.get("per_question",
                                                             []) if q.get("correct")}
        for i, q in enumerate(quiz.get("questions") or []):
            topic_key = quiz.get("topic") or (goal or {}).get("subject")
            self.store.move_topic(topic_key,
                                  "up" if q.get("cite_block") in
                                  correct_blocks else "down")
        row = self.store.add_quiz_result({
            "goal_id": (goal or {}).get("id", ""),
            "topic": quiz.get("topic", ""),
            "score_pct": grade["score_pct"],
            "passed": bool(grade["passed"]),
            "correct": grade["correct"], "total": grade["total"],
            "at": _today_iso()})
        self._snapshot("grade")
        self.store.audit("quiz_graded", score=grade["score_pct"])
        return self._reply(request, engine.render_grade(grade, row["topic"]),
                           action="grade", extra={"result": row,
                                                  "grade": grade})

    def _progress(self, request) -> AgentResult:
        goal = self._goal(request)
        results = self.store.quiz_results(goal.get("id", "") if goal else "")
        mastery = learning.mastery_score(results)
        streak = learning.streak_days(results)
        adapt = learning.next_level((goal or {}).get("level", "beginner")
                                    if goal else "beginner", mastery)
        plan_row = self.store.latest_plan((goal or {}).get("id") or "")
        text = engine.render_progress(goal, mastery, self.store.boxes(),
                                      streak, plan_row, adapt)
        self.store.save_progress({"goal_id": (goal or {}).get("id", ""),
                                  "mastery": mastery, "streak": streak,
                                  "adapt": adapt})
        return self._reply(request, text, action="progress",
                           extra={"mastery": mastery, "streak": streak,
                                  "adapt": adapt})

    def _adapt(self, request) -> AgentResult:
        goal = self._goal(request)
        results = self.store.quiz_results(goal.get("id") if goal else "")
        mastery = learning.mastery_score(results)
        decision = learning.next_level(
            (goal or {}).get("level", "beginner") if goal else "beginner",
            mastery)
        if goal and decision["level"] != goal.get("level"):
            self.store.archive_goal(goal["id"])
            self.store.add_goal(goal["subject"], level=decision["level"],
                                deadline=goal.get("deadline", ""))
            self._snapshot("adapt")
        elif goal:
            self._snapshot("adapt")
        if not goal:
            return self._reply(
                request, "No active goal — set one with 'learn …' and "
                         "the dial needs some graded quizzes "
                         "before it moves.",
                action="adapt", status=TaskStatus.NEEDS_INPUT)
        return self._reply(
            request,
            f"Difficulty: {decision['decision']} → "
            f"[{decision['level']} levels — {learning.level_guidance(decision['level'])}]. "
            f"Reason: {decision['reason']}.",
            action="adapt", extra={"adapt": decision, "mastery": mastery})

    def _prefs(self, request) -> AgentResult:
        meta = request.metadata or {}
        patch = {}
        if "weekly_minutes" in meta:
            patch["weekly_minutes"] = _int(meta.get("weekly_minutes"), 240)
        if "difficulty" in meta:
            d = str(meta.get("difficulty") or "").lower()
            if d in ("easy", "medium", "hard"):
                patch["preferred_difficulty"] = d
        prefs = self.store.set_prefs(patch) if patch else self.store.prefs()
        lines = ["Study preferences:"] + [f"  • {k}: {v}"
                                          for k, v in sorted(prefs.items())]
        if not prefs:
            lines.append("  (none — metadata {weekly_minutes, difficulty})")
        return self._reply(request, "\n".join(lines), action="prefs")

    _HANDLERS = {
        "goal_set": _goal_set,
        "resources": _resources,
        "plan": _plan,
        "plan_done": _plan_done,
        "explain": _explain,
        "quiz": _quiz,
        "grade": _grade,
        "progress": _progress,
        "adapt": _adapt,
        "status": _progress,
        "prefs": _prefs,
    }

    @staticmethod
    def _last_quiz_in_context(request: AgentRequest):
        """The quiz delivered in the previous turn of this conversation
        (context handoff), when it carried one as data."""
        for _role, blob in reversed(request.context or []):
            if isinstance(blob, str) and blob.strip():
                try:
                    row = json.loads(blob)
                except Exception:
                    continue
                if isinstance(row, dict) and row.get("study") \
                        and isinstance(row.get("quiz"), dict):
                    return row["quiz"]
        return None


# ── small text helpers ──────────────────────────────────────────────────

def _verbish(text: str):
    """'learn/study/revise/master X' present? Guards against turning
    'study plan hello how are you' into a goal about a plan."""
    m = _re(r"\b(?:learn|study|revise|master|prepare)\s+(?!plan\b)",
            (text or "").lower())
    return m


def _re(pattern: str, text: str):
    import re as _re_module
    return _re_module.search(pattern, text)


def _after(text: str, pattern: str) -> str:
    m = _re(pattern, (text or "").lower())
    if not m:
        return ""
    return (text or "")[m.end():].strip(" .?!")


def _after_lower(text: str, pattern: str) -> str:
    m = _re(pattern, (text or "").lower())
    if not m:
        return ""
    return text[m.end():].strip(" .?!")


def _letters(text: str) -> list[str]:
    out = []
    for chunk in (text or "").replace("\n", " ").split(","):
        chunk = chunk.strip().upper()
        if chunk in ("A", "B", "C", "D") and chunk not in out:
            out.append(chunk)
    return out


def _int(value, default: int) -> int:
    try:
        return max(1, min(int(value), 10))
    except (TypeError, ValueError):
        return default


def _today_iso() -> str:
    return date.today().isoformat()
