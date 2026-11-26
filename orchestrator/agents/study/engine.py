"""
orchestrator/agents/study/engine.py — plans, explanations, quizzes, progress.

Pure deterministic functions over injected state (results / boxes / goals /
evidence). No store access, no network, no LLM. The agent module calls them
and renders.

  * render_plan / format_plan   — the dated study plan, human-readable
  * extractive_explanation      — the no-LLM explanation floor: verbatim
                                  evidence sentences from the user's own
                                  library (the RAG-strategies philosophy:
                                  unsupported claims are structurally
                                  impossible — the explanation IS the
                                  evidence)
  * parse_quiz / grade_quiz     — quiz build from numbered evidence
    (level-shaped distractors) and deterministic grading that records the
    correct/incorrect list and the pass verdict
  * render_quiz / render_grade
  * render_progress / decide_adapt — the weekly progress report and the
    next adaptive-difficulty move (learning.next_level over the goal's
    graded history)
"""
from __future__ import annotations

import json
import re
from datetime import datetime

from orchestrator.agents.study import learning

LEVELS = learning.LEVELS
BOX_LABELS = learning.BOX_LABELS


# ── plans ────────────────────────────────────────────────────────────────

def format_plan(plan: dict, goal_row: dict | None = None) -> str:
    lines = [f"Study plan — {plan.get('subject', 'your topic')} "
             f"[{plan.get('level', 'beginner')}]"
             + (f" → deadline {plan['deadline']}" if plan.get("deadline")
                else "")]
    if goal_row and goal_row.get("target"):
        lines.append(f"goal: {goal_row['target']}")
    lines.append(f"{plan.get('days', 0)} day(s) — {len(plan.get('items') or [])}"
                 " session(s):")
    for item in (plan.get("items") or [])[:14]:
        mark = "✔" if item.get("done") else "•"
        lines.append(f"  {mark} {item['id']} {item['date']} {item['kind']} "
                     f"— {item['topic']} ({item['minutes']} min)"
                     + ("" if item.get("done") else ""))
    if plan.get("note"):
        lines.append(f"  note: {plan['note']}")
    return "\n".join(lines)


def render_plan(agent_reply: str) -> tuple[str, list[dict]]:
    return agent_reply, []


# ── explanations (grounded, cited, no fabrication) ───────────────────────

def extractive_explanation(topic: str, evidence: str, level: str,
                           citations: list[str]) -> dict:
    """The no-model explanation: verbatim sentences from the retrieved
    library evidence, top 4-5, each cited like [n]. When the evidence says
    nothing on the topic the reply says the base is thin — it never
    invents content."""
    text = (evidence or "").strip()
    if not text:
        return {"ok": False,
                "text": f"I don't have anything on '{topic}' in your "
                        "study library yet — add notes or a textbook, or "
                        "ask me to find external resources.",
                "citations": [],
                "grounded": False}
    sentences: list[tuple[int, str]] = []
    block_no = 0
    for raw_block in re.split(r"\n\n+", text):
        for line in raw_block.splitlines():
            line = line.strip()
            m = re.match(r"^\[(\d+)\]\s*(.*)$", line)
            if m:
                block_no = int(m.group(1))
                rest = m.group(2)
                continue
            if not line or not rest:
                continue
            for sent in re.split(r"(?<=[.!?])\s+(?=[A-Z0-9])", line):
                sent = sent.strip()
                if len(sent.split()) >= 6 and (
                        topic.lower() in sent.lower() or
                        any(w in sent.lower() for w in
                            _keywords(topic))):
                    citations_idx = max(citations and 0 or 0, 0)
                    sentences.append((block_no or len(citations or [1]),
                                      sent))
                if len(sentences) >= 5:
                    break
            rest = ""
        if len(sentences) >= 5:
            break
    out = []
    seen = set()
    for idx, sent in sentences:
        k = re.sub(r"\W+", "", sent.lower())[:100]
        if k in seen:
            continue
        seen.add(k)
        out.append(f"{sent} [{idx}]")
    if not out:
        return {"ok": False,
                "text": f"Your library's passages on '{topic}' don't have "
                        "a clean sentence to quote — the evidence is "
                        "there but too dense for an extractive "
                        "explanation.",
                "citations": citations[:5],
                "grounded": False}
    return {"ok": True, "text": "\n".join(out), "citations": citations[:5],
            "grounded": True,
            "level_note": f"level: {level} — {learning.level_guidance(level)}"}


def _keywords(topic: str) -> list[str]:
    stop = {"the", "a", "an", "of", "for", "to", "in", "and", "or", "on",
            "from", "my", "me", "please", "explain", "about"}
    words = [w for w in re.findall(r"[a-z0-9]{3,}", (topic or "").lower())
             if w not in stop]
    return words[:4]


# ── quizzes ──────────────────────────────────────────────────────────────

def build_quiz(topic: str, level: str, evidence: str,
               citations: list[str], n: int = 5) -> dict:
    """A grounded quiz: `n` questions built from the numbered evidence —
    the correct answer is a verbatim evidence sentence (or a cloze of it),
    distractors come from OTHER evidence blocks. No evidence → no quiz,
    honestly."""
    blocks = _numbered_quotes(evidence)
    informative = [b for b in blocks if len(b[1].split()) >= 8]
    if not informative:
        return {"ok": False,
                "error": "no citation-backed material in your study library "
                         "for a quiz on this topic"}
    questions, pool = [], list(informative)
    for i in range(min(max(1, n), 6)):
        if len(pool) < 2 and i > 0 and not questions:
            break
        idx, correct = pool.pop(0)
        distractors: list[str] = []
        for j, (jdx, text) in enumerate(pool + list(informative)):
            if jdx != idx and text[:60] != correct[:60]:
                distractors.append(text)
            if len(distractors) >= 3:
                break
        distractors = distractors[:3]
        options = distractors + [correct]
        # deterministic order: shuffle by index-kind
        options = options[::-1] if (idx % 2 == 0) else options
        letter_correct = "ABCD"[options.index(correct)] \
            if correct in options else "A"
        question_text = (f"Which statement matches your study material on "
                         f"'{_short(topic)}'?" if not _is_number(correct)
                         else f"Which statement gives the figures for "
                              f"'{_short(topic)}'?")
        if level == "beginner" and _is_number(correct):
            question_text = (f"Which statement gives the correct figures "
                             f"for '{_short(topic)}' featured in your "
                             "notes?")
        questions.append({
            "id": f"q{i + 1}", "kind": "multiple_choice",
            "question": question_text,
            "options": {c: o for c, o in zip("ABCD", options)},
            "answer": letter_correct,
            "cite_block": idx,
        })
        if not pool:
            pool = list(informative[1:]) or list(informative)
    if not questions:
        return {"ok": False, "error": "could not build a grounded quiz"}
    return {"ok": True, "topic": _short(topic), "level": level,
            "questions": questions, "citations": citations[:5]}


def grade_quiz(quiz: dict, answers: dict) -> dict:
    """Deterministic grading of one attempt.

    answers: {question_id: letter}. Graded against quiz['questions']' answer
    keys only — never against the quiz text itself. Returns score, pass
    verdict (≥60%), per-question correct/incorrect with the right letters
    and their citations.
    """
    questions = quiz.get("questions") or []
    answers = {str(k).strip().lower(): str(v).strip().upper()
               for k, v in (answers or {}).items()}
    per_q, correct = [], 0
    for q in questions:
        given = answers.get(str(q.get("id", "")).lower(), "")
        key = str(q.get("answer", "")).upper()
        ok = given == key and bool(key)
        per_q.append({"id": q.get("id"), "given": given, "key": key,
                      "correct": ok,
                      "cite_block": q.get("cite_block")})
        correct += 1 if ok else 0
    total = max(1, len(questions))
    score = round(100.0 * correct / total)
    return {"correct": correct, "total": len(questions), "score_pct": score,
            "passed": score >= 60, "per_question": per_q,
            "citations": quiz.get("citations", [])}


def render_quiz(quiz: dict) -> str:
    lines = [f"Quiz — {quiz.get('topic', '')} [{quiz.get('level', '')}] "
             f"({len(quiz.get('questions') or [])} questions, from your "
             "study library):"]
    for q in quiz.get("questions") or []:
        lines.append(f"  {q['id']}. {q['question']}")
        for letter in "ABCD":
            opt = q["options"].get(letter)
            if opt:
                text = opt if len(opt) <= 110 else opt[:107] + "…"
                lines.append(f"       {letter}) {text}")
    lines.append("Answer like: A,B,C,D… per question (or metadata "
                 "{answers: {q1: 'A', …}}). I grade it and update "
                 "your progress.")
    return "\n".join(lines)


def render_grade(grade: dict, topic: str = "") -> str:
    lines = [f"Quiz result — {topic or 'quiz'}: {grade['correct']}/"
             f"{grade['total']} ({grade['score_pct']}%) — "
             f"{'PASSED' if grade['passed'] else 'not passed yet'}"]
    for q in grade.get("per_question", []):
        mark = "✔" if q["correct"] else "✘"
        lines.append(f"  {mark} {q['id']}: entered {q['given'] or '—'}, "
                     f"answer {q['key']}"
                     + (f" — see citation [{q['cite_block']}]"
                        if not q["correct"] else ""))
    lines.append("Repeat the questions you missed after re-reading the "
                 "cited passages — that's what moves them up a box.")
    return "\n".join(lines)


# ── progress + adapt rendering ──────────────────────────────────────────

def render_progress(goal_row: dict | None, mastery: dict,
                    boxes: dict, streak: int, plan_row: dict | None,
                    adapt: dict) -> str:
    g = goal_row or {}
    lines = [f"Study status — {g.get('subject') or 'no goal yet'} "
             f"[{g.get('level') or 'beginner'}]"
             + (f" (deadline {g['deadline']})" if g.get("deadline") else "")]
    if not goal_row:
        lines.append("  'learn linear algebra' sets a goal; then everything "
                     "below fills in.")
    if plan_row:
        items = plan_row.get("items") or []
        done = sum(1 for i in items if i.get("done"))
        lines.append(f"  plan: {done}/{len(items)} session(s) done")
        upcoming = [i for i in items if not i.get("done")][:2]
        for up in upcoming:
            lines.append(f"   next: {up['date']} {up['kind']} — {up['topic']}")
    if mastery.get("attempts"):
        lines.append(f"  quizzes: {mastery['passed']}/{mastery['attempts']} "
                     f"passed ({'' if mastery['score'] is None else round(mastery['score'] * 100)}% mastery-weighted), "
                     f"streak {mastery['streak_correct']} correct in a row")
    if boxes:
        rated = [(t, e.get("box")) for t, e in boxes.items() if
                 isinstance(e, dict) and e.get("box")]
        if rated:
            rated.sort(key=lambda x: -x[1])
            top = ", ".join(f"{'box'+str(b)} {t[:30]}" for t, b in rated[:4])
            lines.append(f"  ladder: {top}")
    if streak:
        lines.append(f"  study streak: {streak} day(s)")
    lines.append(f"  adapt: {adapt.get('decision')} at level "
                 f"{adapt.get('level')} — {adapt.get('reason')}")
    return "\n".join(lines)


# ── helpers ──────────────────────────────────────────────────────────────

def _numbered_quotes(evidence: str) -> list[tuple[int, str]]:
    """(block_no, sentence) pairs from a '[n] cite\n' + body' evidence
    string, split to informative sentences first."""
    out: list[tuple[int, str]] = []
    cur_no, cur_text = 0, ""
    for grp in re.split(r"\n\n+", (evidence or "").strip()):
        for line in grp.splitlines():
            line = line.strip()
            if not line:
                continue
            m = re.match(r"^\[(\d+)\]\s*(.*)$", line)
            if m:
                for s in _sentences(cur_text):
                    out.append((cur_no or 1, s))
                cur_no, cur_text = int(m.group(1)), m.group(2)
                continue
            cur_text += " " + line
        for s in _sentences(cur_text):
            out.append((cur_no or 1, s))
        cur_text = ""
    for s in _sentences(cur_text):
        out.append((cur_no or 1, s))
    out = [o for o in out if len(o[1].split()) >= 8]
    seen, dedup = set(), []
    for n, s in out:
        k = re.sub(r"\W+", "", s.lower())[:100]
        if k not in seen:
            seen.add(k)
            dedup.append((n, s))
    return dedup


def _sentences(text: str) -> list[str]:
    out = []
    for s in re.split(r"(?<=[.!?])\s+(?=[A-Z0-9])", (text or "").strip()):
        s = s.strip()
        if len(s.split()) >= 8:
            out.append(s)
    return out


def _short(topic: str, cap: int = 60) -> str:
    t = (topic or "").strip()
    return t if len(t) <= cap else t[:cap - 1] + "…"


def _is_number(s: str) -> bool:
    return bool(re.search(r"\d", (s or "")))
